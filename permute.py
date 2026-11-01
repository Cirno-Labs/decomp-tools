"""Searches source variants of one function for a byte match.

    permute.py NAME [-j JOBS] [-t SECONDS] [-s SEED] [--start FILE] [--file]
    permute.py NAME --sweep [-t SECONDS] [--file]
    permute.py NAME --decls [-j JOBS] [--limit N]
    permute.py NAME --pad | --plan [--limit N]

NAME is a function's name in config/symbols.csv (`Stage::DrawLayer`),
already defined in src/ and building. The permuter takes its
definition, applies random rewrites (reordering local declarations,
swapping the operands of commutative operators, comparisons, && and ||,
swapping and moving adjacent statements, flipping if/else, writing
`x += y` as `x = x + y`, `x++` as `++x` or `x += 1`, `!x` as `x == 0`,
`for` as `while`, `x = c ? a : b` as an if/else, `&a[i]` as `a + i`,
`x = e; return x;` as `return e;`, adding or dropping `default: break;`,
and back), and compiles each variant on its own with the file's flags
(build.cflags and its `// cflags:` line), without linking. A variant is
scored by the instructions that differ from the original, as compare.py
cuts and relocates the function (file-static functions by their symbol,
jump tables cut off), plus 1000 for each other function of the file that
matched before and no longer does. The search climbs towards lower
scores until it finds 0 or runs out of time. A rewrite may change the
meaning (swapping dependent statements, or && operands with side
effects); a variant that matches byte for byte is proven correct by the
match itself. The rewrites expect the sources' layout: braces on their
own lines, four-space indents, one statement per line.

Most body rewrites must touch the lines whose code differs: a compile
with /Zd maps the differing instructions to source lines through the
object's COFF line numbers (MSVC 7.1 still writes them; /Zd changes no
code). Each rewrite kind keeps a tally of how often it lowered the
score, in build/permute/NAME.stats.json, and is picked by it; farm.py
--learn seeds the tallies from how the best variants so far differ
from src/.

--start FILE searches from the variant in FILE instead, when it builds
and scores better than the definition in src/.

--file also lets the search move the file's top-level declaration lines
(externs and prototypes) within their runs, keeping the file's other
functions matching; such a variant goes to build/permute/NAME.file
whole.

--sweep enumerates instead of sampling, around the lines whose code
differs: every declaration order, every combination of swapped operands
in those lines, every single application of the other rewrites there,
and with --file every place a top-level declaration before the function
can move to. It keeps the best two variants and goes again from them
until a whole pass finds nothing better, then prints "sweep exhausted".

--decls searches the local declarations only, exhaustively: every order
of the declaration lines at the top of the function, times every order
of the names within each line (sampled down to --limit candidates).
MSVC 7.1 assigns stack slots by declaration order, and the order can
sway which register a local gets, so an order the random search rarely
hits can decide a slot or a register; VC5, where this mode comes from,
orders the terms of a sum by it too.

--pad and --plan look for a shift of the compiler's symbol IDs: VC5
orders terms and registers by a key built from the order symbols are
declared in, so adding N dummy declarations before the function can
make it match. --pad tries N below --limit (default 32), then, if one
does, every real change of the declarations before the function that
shifts the IDs the same way (a line moved across the function, a
prototype's parameter names dropped); with none, it searches the local
declaration orders at the three closest shifts. --plan finds the shifts
that make each function of NAME's file match (below --limit, default
64) and prints how many declarations to add before each one. MSVC 7.1
has not shown the effect: the shift line --pad prints was flat for
th09's functions tried.

Under wine, a wineserver is kept running until 30 seconds after the
last compile, as starting one for each compile takes several times as
long as the compile itself.

src/ is never written. The best variant goes to build/permute/NAME.c,
and the change is printed as a diff to apply by hand; run the full
compare after applying it, since a change can flip other functions.
"""
import argparse, difflib, os, random, re, shutil, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor

import build, coff, compare, project

ROOT = project.ROOT
os.chdir(ROOT)

OUT = "build/permute"
COMPARE_LOCK = threading.Lock()  # results() reads build.sources, patched per call
FILE_MODE = [False, None]  # --file, and the text around the function in src/
COMMUTATIVE = ["+", "*", "&", "|", "^", "==", "!="]
FLIPPED = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}


def stem(name):
    """The file name stem of function `name`'s outputs: a C++ name's `::`
    becomes `-`, so it also names a file on Windows."""
    return re.sub(r"[^\w-]", "_", name.replace("::", "-"))


def out_path(name, ext):
    return os.path.join(OUT, stem(name) + ext)


def keep(name, variant):
    """Writes the best variant so far, so a stopped search keeps it: the
    function's body as NAME.c and, when the rest of the file changed too,
    the whole file as NAME.file."""
    before, body, after = variant
    os.makedirs(OUT, exist_ok=True)
    open(out_path(name, ".c"), "w", encoding="utf-8", newline="\n").write(body)
    whole = out_path(name, ".file")
    if FILE_MODE[0] and (before, after) != FILE_MODE[1]:
        open(whole, "w", encoding="utf-8", newline="\n").write(before + body + after)
    elif os.path.exists(whole):
        os.remove(whole)


def find_function(name):
    """(path, text, start, end) of `name`'s definition in src/."""
    for path in sorted(build.sources()):
        src = path[0]
        if src.replace("\\", "/").startswith("src/dll/"):
            continue
        text = open(src, encoding="utf-8").read()
        m = re.search(r"^[^\n;{}()#/]*(?<![\w:~])" + re.escape(name) + r"\s*\([^;{]*\)\s*\{", text, re.M)
        if not m:
            continue
        depth, k = 0, m.end() - 1
        while k < len(text):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            k += 1
            if depth == 0:
                break
        return src, text, m.start(), k
    sys.exit(f"{name}: no definition in src/")


OPERAND = r"[A-Za-z_][\w]*(?:(?:\.|->)[A-Za-z_]\w*|\[[^\[\]]*\])*|\d+|0x[0-9a-fA-F]+"
LEFT_EDGE = r"(?<=[(\[,=?:])\s*|(?<=return )|(?<=&& )|(?<=\|\| )"


def operand_pattern():
    ops = "|".join(re.escape(o) for o in sorted(COMMUTATIVE + list(FLIPPED), key=len, reverse=True))
    return re.compile(r"(?:" + LEFT_EDGE + r")(" + OPERAND + r") (" + ops + r") (" + OPERAND
                      + r")(?=\s*[)\],;?:]|\s*&&|\s*\|\|)")


def swap_operands(body, rng):
    """Swaps the two operands of one commutative operator, or flips one
    comparison, where both are simple and bounded by low-precedence
    tokens."""
    hits = list(operand_pattern().finditer(body))
    if not hits:
        return None
    m = rng.choice(hits)
    a, op, b = m.group(1), m.group(2), m.group(3)
    op = FLIPPED.get(op, op)
    return body[:m.start(1)] + f"{b} {op} {a}" + body[m.end(3):]


def top_lines_of(lines, decl, keywords=("return", "goto", "case", "else", "do", "delete", "throw")):
    """The indices of the declaration lines (matching `decl`) at the top
    of a function's body, after the line holding its opening brace.
    Comment lines among them are skipped; the first other line ends
    them."""
    first = next((i + 1 for i, l in enumerate(lines) if l.strip() == "{" or l.endswith(") {")), 1)
    top = []
    for i in range(first, len(lines)):
        line = lines[i]
        if not line.strip() or line.strip().startswith("//"):
            if top:
                break
            continue
        m = decl.match(line)
        if not m or line.split()[0] in keywords:
            break
        top.append(i)
    return top


def reorder_declarations(body, rng):
    """Shuffles the names in one multi-name declaration, or swaps two
    adjacent declaration lines at the top of the function."""
    lines = body.split("\n")
    decl = DECL
    top = top_lines_of(lines, decl)
    if not top:
        return None
    if len(top) > 1 and rng.random() < 0.5:
        i = rng.choice(top[:-1])
        if i + 1 in top:
            lines[i], lines[i + 1] = lines[i + 1], lines[i]
            return "\n".join(lines)
    i = rng.choice(top)
    m = decl.match(lines[i])
    names = declared_names(m.group(3))
    if len(names) < 2:
        return None
    rng.shuffle(names)
    lines[i] = f"{m.group(1)}{m.group(2)}{', '.join(names)};"
    return "\n".join(lines)


SIMPLE = re.compile(r"^(\s+)[^\s{}][^{}]*;$")
CONTROL = re.compile(r"^\s*(if|else|for|while|do|switch|case|default|return|break|continue|goto)\b")


def swap_statements(body, rng):
    """Swaps two adjacent simple statements at the same indent."""
    lines = body.split("\n")
    pairs = [i for i in range(1, len(lines) - 1)
             if SIMPLE.match(lines[i]) and SIMPLE.match(lines[i + 1])
             and not CONTROL.match(lines[i]) and not CONTROL.match(lines[i + 1])
             and SIMPLE.match(lines[i]).group(1) == SIMPLE.match(lines[i + 1]).group(1)
             and not re.match(r"^\s+[A-Za-z_][\w\s\*]*\s\**[A-Za-z_]\w*(\s*,\s*\**\w+)*;$", lines[i])]
    if not pairs:
        return None
    i = rng.choice(pairs)
    lines[i], lines[i + 1] = lines[i + 1], lines[i]
    return "\n".join(lines)


def indent_of(line):
    return line[:len(line) - len(line.lstrip())]


def block_at(lines, i):
    """(open, close) of the block the header at line `i` controls, braces
    on their own lines at its indent: the lines of the brace pair, or
    the one statement one level deeper twice; None otherwise."""
    ind = indent_of(lines[i])
    if i + 1 >= len(lines):
        return None
    if lines[i + 1] == ind + "{":
        for j in range(i + 2, len(lines)):
            if lines[j] == ind + "}":
                return i + 1, j
            if lines[j].strip() and not lines[j].startswith(ind + " "):
                return None
        return None
    nxt = lines[i + 1]
    if nxt.startswith(ind + "    ") and nxt[len(ind) + 4:][:1] not in ("", " ", "{") and nxt.endswith(";"):
        return i + 1, i + 1
    return None


def inner(lines, block):
    """The statement lines of a block from block_at."""
    a, b = block
    return lines[a:a + 1] if a == b else lines[a + 1:b]


def braced(ind, stmts):
    return [ind + "{", *stmts, ind + "}"]


INVERSE = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}


def negate(cond):
    """`!(cond)`, with a single comparison inverted instead."""
    if cond.startswith("!(") and cond.endswith(")") and balanced(cond[2:-1]):
        return cond[2:-1]
    m = re.fullmatch(r"(" + OPERAND + r") (==|!=|<=|>=|<|>) (" + OPERAND + r")", cond)
    if m:
        return f"{m.group(1)} {INVERSE[m.group(2)]} {m.group(3)}"
    if re.fullmatch(OPERAND, cond):
        return "!" + cond
    return f"!({cond})"


def balanced(text):
    depth = 0
    for c in text:
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth < 0:
            return False
    return depth == 0


def flip_if(body, rng):
    """Turns `if (c) A else B` into `if (!(c)) B else A`, where B is not
    an `else if`."""
    lines = body.split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)if \((.*)\)$", line)
        if not m or not balanced(m.group(2)):
            continue
        a = block_at(lines, i)
        if not a or a[1] + 1 >= len(lines) or lines[a[1] + 1] != m.group(1) + "else":
            continue
        b = block_at(lines, a[1] + 1)
        if b:
            hits.append((i, m, a, b))
    if not hits:
        return None
    i, m, a, b = rng.choice(hits)
    ind = m.group(1)
    new = [f"{ind}if ({negate(m.group(2))})", *braced(ind, inner(lines, b)), ind + "else",
           *braced(ind, inner(lines, a))]
    return "\n".join(lines[:i] + new + lines[b[1] + 1:])


def swap_logical(body, rng):
    """Swaps the two sides of one && or || between parentheses."""
    side = r"[^()&|\n]+(?:\([^()\n]*\))?[^()&|\n]*"
    pat = re.compile(r"\((" + side + r") (&&|\|\|) (" + side + r")\)")
    hits = list(pat.finditer(body))
    if not hits:
        return None
    m = rng.choice(hits)
    return body[:m.start()] + f"({m.group(3).strip()} {m.group(2)} {m.group(1).strip()})" + body[m.end():]


def compound(body, rng):
    """Writes `x op= y;` as `x = x op y;`, or the other way round."""
    lv = r"[A-Za-z_]\w*(?:(?:\.|->)[A-Za-z_]\w*|\[[^\[\]]*\])*"
    short = re.compile(r"(?m)^(\s+)(" + lv + r") ([-+*&|^]|<<|>>)= ([^;]+);$")
    long = re.compile(r"(?m)^(\s+)(" + lv + r") = (" + lv + r") ([-+*&|^]|<<|>>) ([^;]+);$")
    hits = [("s", m) for m in short.finditer(body)] + [("l", m) for m in long.finditer(body) if m.group(2) == m.group(3)]
    if not hits:
        return None
    kind, m = rng.choice(hits)
    if kind == "s":
        new = f"{m.group(1)}{m.group(2)} = {m.group(2)} {m.group(3)} {m.group(4)};"
    else:
        new = f"{m.group(1)}{m.group(2)} {m.group(4)}= {m.group(5)};"
    return body[:m.start()] + new + body[m.end():]


def shuffle_declarations(body, rng):
    """Shuffles every declaration line at the top of the function."""
    lines = body.split("\n")
    decl = DECL
    top = top_lines_of(lines, decl)
    if len(top) < 2:
        return None
    picked = [lines[i] for i in top]
    rng.shuffle(picked)
    for i, line in zip(top, picked):
        lines[i] = line
    return "\n".join(lines)


def inline_return(body, rng):
    """Writes `x = e; return x;` as `return e;`, or the other way round
    through the variable a return's value was last stored in."""
    stored = re.compile(r"(?m)^(\s+)([A-Za-z_]\w*) = ([^;]+);\n\1return \2;$")
    direct = re.compile(r"(?m)^(\s+)return ([^;]+);$")
    hits = [("s", m) for m in stored.finditer(body)]
    names = re.findall(r"(?m)^\s+(?:[A-Za-z_]\w*\s+)+\**([A-Za-z_]\w*)(?:, \**[A-Za-z_]\w*)*;$", body)
    params = re.findall(r"([A-Za-z_]\w*)\s*[,)]", body.split("{", 1)[0])
    hits += [("d", m) for m in direct.finditer(body) if names or params]
    if not hits:
        return None
    kind, m = rng.choice(hits)
    if kind == "s":
        new = f"{m.group(1)}return {m.group(3)};"
    else:
        var = rng.choice(names + params)
        new = f"{m.group(1)}{var} = {m.group(2)};\n{m.group(1)}return {var};"
    return body[:m.start()] + new + body[m.end():]


LVALUE = r"[A-Za-z_]\w*(?:(?:\.|->)[A-Za-z_]\w*|\[[^\[\]]*\])*"


def condition_form(body, rng):
    """Writes one test as `!x` or `x == 0`, or `if (x)` as `if (x != 0)`,
    and back."""
    forms = [
        (re.compile(r"(?<=[(\s])!(" + OPERAND + r")(?=[)\s])"), lambda m: f"{m.group(1)} == 0"),
        (re.compile(r"(?<=\()(" + OPERAND + r") == 0(?=\))|(?<=[&|] )(" + OPERAND + r") == 0(?=[)\s])"),
         lambda m: f"!{m.group(1) or m.group(2)}"),
        (re.compile(r"(?:(?<=\bif \()|(?<=\bwhile \())(" + OPERAND + r")(?=\))"), lambda m: f"{m.group(1)} != 0"),
        (re.compile(r"(?:(?<=\bif \()|(?<=\bwhile \())(" + OPERAND + r") != 0(?=\))"), lambda m: m.group(1)),
    ]
    hits = [(m, f) for pat, f in forms for m in pat.finditer(body)]
    if not hits:
        return None
    m, f = rng.choice(hits)
    return body[:m.start()] + f(m) + body[m.end():]


def increment_form(body, rng):
    """Writes the statement `x++;` as `++x;` or `x += 1;`, and back."""
    pat = re.compile(r"(?m)^(\s+)(?:(" + LVALUE + r")(\+\+|--)|(\+\+|--)(" + LVALUE + r")|(" + LVALUE
                     + r") ([-+])= 1);$")
    hits = list(pat.finditer(body))
    if not hits:
        return None
    m = rng.choice(hits)
    ind = m.group(1)
    if m.group(2):
        x, op = m.group(2), m.group(3)
    elif m.group(5):
        x, op = m.group(5), m.group(4)
    else:
        x, op = m.group(6), m.group(7) * 2
    forms = [f"{x}{op};", f"{op}{x};", f"{x} {op[0]}= 1;"]
    forms.remove(m.group(0)[len(ind):])
    return body[:m.start()] + ind + rng.choice(forms) + body[m.end():]


def ternary_form(body, rng):
    """Writes `x = c ? a : b;` as an if/else of two assignments, and back."""
    lines = body.split("\n")
    tern = re.compile(r"^(\s+)(" + LVALUE + r") = \(?([^;?]+?)\)? \? ([^;:]+) : ([^;]+);$")
    hits = []
    for i, line in enumerate(lines):
        m = tern.match(line)
        if m and balanced(m.group(3)):
            hits.append(("t", i, i, m))
        m = re.match(r"^(\s*)if \((.*)\)$", line)
        if not m or not balanced(m.group(2)):
            continue
        a = block_at(lines, i)
        if not a or a[1] + 1 >= len(lines) or lines[a[1] + 1] != m.group(1) + "else":
            continue
        b = block_at(lines, a[1] + 1)
        if not b or len(inner(lines, a)) != 1 or len(inner(lines, b)) != 1:
            continue
        x = re.match(r"^\s+(" + LVALUE + r") = ([^;]+);$", inner(lines, a)[0])
        y = re.match(r"^\s+(" + LVALUE + r") = ([^;]+);$", inner(lines, b)[0])
        if x and y and x.group(1) == y.group(1):
            hits.append(("i", i, b[1], (m.group(1), m.group(2), x.group(1), x.group(2), y.group(2))))
    if not hits:
        return None
    kind, i, j, m = rng.choice(hits)
    if kind == "t":
        ind, x = m.group(1), m.group(2)
        new = [f"{ind}if ({m.group(3)})", *braced(ind, [f"{ind}    {x} = {m.group(4)};"]), ind + "else",
               *braced(ind, [f"{ind}    {x} = {m.group(5)};"])]
    else:
        ind, cond, x, a, b = m
        new = [f"{ind}{x} = ({cond}) ? {a} : {b};"]
    return "\n".join(lines[:i] + new + lines[j + 1:])


def loop_form(body, rng):
    """Writes `for (a; c; s) {...}` as `a; while (c) {...; s;}`, and back,
    for braced loops with no continue."""
    lines = body.split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s+)for \(([^;]*); ([^;]+); ([^;]*)\)$", line)
        blk = block_at(lines, i)
        if m and blk and blk[0] != blk[1] and balanced(m.group(4)) \
                and not any("continue" in l for l in lines[blk[0]:blk[1]]):
            hits.append(("f", i, blk, m))
        m = re.match(r"^(\s+)while \((.+)\)$", line)
        if m and i > 0 and blk and blk[0] != blk[1]:
            ind = m.group(1)
            init = re.match(r"^" + ind + r"([^\s{}][^{};]*);$", lines[i - 1])
            step = re.match(r"^" + ind + r"    ([^\s{}][^{};]*);$", lines[blk[1] - 1])
            if init and step and not CONTROL.match(lines[i - 1]) and not CONTROL.match(lines[blk[1] - 1]) \
                    and not any("continue" in l for l in lines[blk[0]:blk[1]]):
                hits.append(("w", i, blk, (m, init.group(1), step.group(1))))
    if not hits:
        return None
    kind, i, (a, b), m = rng.choice(hits)
    if kind == "f":
        ind, init, cond, step = m.group(1), m.group(2), m.group(3), m.group(4)
        new = ([f"{ind}{init};"] if init.strip() else []) + [f"{ind}while ({cond})"] + lines[a:b]
        new += ([f"{ind}    {step};"] if step.strip() else []) + [f"{ind}}}"]
        return "\n".join(lines[:i] + new + lines[b + 1:])
    w, init, step = m
    ind = w.group(1)
    new = [f"{ind}for ({init}; {w.group(2)}; {step})"] + lines[a:b - 1] + [f"{ind}}}"]
    return "\n".join(lines[:i - 1] + new + lines[b + 1:])


def address_form(body, rng):
    """Writes `&a[i]` as `a + i`, and back."""
    amp = re.compile(r"&([A-Za-z_]\w*)\[([^\[\]]+)\](?![\[.]|->)")
    plus = re.compile(r"(?<=[(=,] )([A-Za-z_]\w*) \+ (" + OPERAND + r")(?=[);,])|(?<=\()([A-Za-z_]\w*) \+ (" + OPERAND
                      + r")(?=\))")
    hits = [("a", m) for m in amp.finditer(body)] + [("p", m) for m in plus.finditer(body)]
    if not hits:
        return None
    kind, m = rng.choice(hits)
    if kind == "a":
        return body[:m.start()] + f"{m.group(1)} + {m.group(2)}" + body[m.end():]
    a, i = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return body[:m.start()] + f"&{a}[{i}]" + body[m.end():]


def move_statement(body, rng):
    """Moves one simple statement up or down past up to three others at
    the same indent."""
    lines = body.split("\n")

    def simple(i):
        m = SIMPLE.match(lines[i])
        return m and not CONTROL.match(lines[i]) and not re.match(r"^\s+[A-Za-z_][\w\s\*]*\s\**[A-Za-z_]\w*(\s*,\s*\**\w+)*;$",
                                                                    lines[i]) and m.group(1)
    runs, i = [], 1
    while i < len(lines):
        ind = simple(i)
        j = i
        while ind and j + 1 < len(lines) and simple(j + 1) == ind:
            j += 1
        if j - i >= 2:
            runs.append((i, j))
        i = j + 1
    if not runs:
        return None
    a, b = rng.choice(runs)
    i = rng.randint(a, b)
    k = min(b, max(a, i + rng.choice([-3, -2, 2, 3])))
    if k == i:
        return None
    line = lines.pop(i)
    lines.insert(k, line)
    return "\n".join(lines)


def default_case(body, rng):
    """Adds `default: break;` to a switch without a default, at its start
    or its end, or removes an empty one."""
    lines = body.split("\n")
    hits = []
    for i, line in enumerate(lines):
        if not re.match(r"^\s*switch \(.*\)$", line):
            continue
        blk = block_at(lines, i)
        if not blk or blk[0] == blk[1]:
            continue
        a, j = blk
        labels = [l for l in lines[a + 1:j] if re.match(r"^\s*(case .*|default):$", l)]
        if not labels:
            continue
        ind = indent_of(labels[0])
        empty = [k for k in range(a + 1, j - 1) if lines[k] == ind + "default:" and lines[k + 1] == ind + "    break;"
                 and (k + 2 == j or lines[k + 2].startswith(ind + "case "))]
        if empty:
            hits.append(("del", empty[0], ind))
        elif not any(lines[k] == ind + "default:" for k in range(a + 1, j)):
            hits += [("add", j, ind), ("add", a + 1, ind)]
    if not hits:
        return None
    kind, k, ind = rng.choice(hits)
    if kind == "del":
        del lines[k:k + 2]
    else:
        lines[k:k] = [ind + "default:", ind + "    break;"]
    return "\n".join(lines)


TOP_DECL = re.compile(r"^(?!\}|#|typedef struct|struct |union |enum )[A-Za-z_][^\n{}]*;$")


def lift(line, cpp):
    """`line`, taken out of an extern "C" block, as a top-level line."""
    if cpp and not line.startswith('extern "C"'):
        return 'extern "C" ' + line
    return line


def top_lines(text):
    """(index, line, in extern "C" block) of the top-level declaration
    lines in text.split("\\n")."""
    out, depth, extern_block = [], 0, False
    for i, line in enumerate(text.split("\n")):
        top = depth == 0 or (depth == 1 and extern_block)
        if top and TOP_DECL.match(line):
            out.append((i, line, depth == 1))
        if depth == 0:
            extern_block = line.startswith('extern "C" {')
        depth += line.count("{") - line.count("}")
    return out


def unnamed(line):
    """A prototype line with its parameter names dropped, or None."""
    m = re.match(r"^(.*\()(.*)(\)\s*;)$", line)
    if not m or "(" in m[2] or not m[2].strip() or m[2].strip() == "void":
        return None
    params = []
    for p in m[2].split(","):
        q = re.match(r"^(.*?[\s*&])([A-Za-z_]\w*)(\s*\[[^\]]*\])?\s*$", p.strip())
        if not q or not q[1].strip():
            return None
        params.append(q[1].rstrip() + (q[3] or "").strip())
    new = m[1] + ", ".join(params) + m[3]
    return None if new == line else new


def pad_moves(variant, cpp):
    """Every real change of the number of declarations before the
    function: (description, variant)."""
    before, body, after = variant
    out = []
    blines, alines = before.split("\n"), after.split("\n")
    for i, line, inside in top_lines(before):
        rest = blines[:i] + blines[i + 1:]
        moved = lift(line, cpp) if inside else line
        out.append((f"move below: {line}", ("\n".join(rest), body, after[:1] + moved + "\n" + after[1:]
                                            if after.startswith("\n") else "\n" + moved + "\n" + after)))
        new = unnamed(line)
        if new:
            out.append((f"drop the names: {line}", ("\n".join(blines[:i] + [new] + blines[i + 1:]), body, after)))
    for i, line, inside in top_lines(after):
        rest = alines[:i] + alines[i + 1:]
        moved = lift(line, cpp) if inside else line
        out.append((f"move above: {line}", (before + moved + "\n", body, "\n".join(rest))))
    return out


def pad(s, base, limit, jobs):
    """--pad: the variant that matches, or None."""
    cpp = s.src.lower().endswith((".cpp", ".cc", ".cxx"))
    before, body, after = base
    pads = [(before + "".join(f"extern int Pad{i};\n" for i in range(n)), body, after) for n in range(limit)]
    with ThreadPoolExecutor(jobs) as pool:
        scores = list(pool.map(s.score, pads))
    hits = [n for n, sc in enumerate(scores) if sc is not None and sc % 1000 == 0]
    print("shifts: " + " ".join(f"{n}:{'-' if sc is None else sc % 1000}" for n, sc in enumerate(scores)), flush=True)
    if not hits:
        print("no shift of the symbol IDs makes it match", flush=True)
        ranked = sorted((sc % 1000, n) for n, sc in enumerate(scores) if sc is not None)[:3]
        for sc, n in ranked:
            orders = declaration_orders(body, 3000, random.Random(n))[1:]
            print(f"with {n} more declarations (score {sc}): {len(orders)} local declaration orders", flush=True)
            cands = [(pads[n][0], b, after) for b in orders]
            with ThreadPoolExecutor(jobs) as pool:
                for cand, got in zip(cands, pool.map(s.score, cands)):
                    if got is not None and got % 1000 == 0:
                        print(f"MATCH with {n} more declarations before it and this local order:", flush=True)
                        sys.stdout.writelines(difflib.unified_diff(body.splitlines(True), cand[1].splitlines(True),
                                                                   "as written", "matching", n=0))
                        keep(s.name, (before, cand[1], after))
                        return None
        return None
    print(f"it matches with {', '.join(map(str, hits))} more declarations before it", flush=True)
    moves = pad_moves(base, cpp)
    print(f"{len(moves)} declaration changes to try", flush=True)
    with ThreadPoolExecutor(jobs) as pool:
        for (what, variant), sc in zip(moves, pool.map(lambda m: s.score(m[1]), moves)):
            if sc == 0:
                print(f"MATCH: {what}", flush=True)
                return variant
    print("no single declaration change does it")
    return None


def plan(src, text, limit, jobs):
    """--plan: prints how many declarations to add before each function
    in `src` to match the most of them."""
    rows = {r["name"] for r in compare_rows()}
    import reorder
    names = []
    for kind, item in reorder.items(text):
        n = reorder.fn_name(item) if kind == "fn" else None
        if n in rows and n not in names:
            names.append(n)
    hits = {}
    for name in names:
        try:
            _, t, start, end = find_function(name)
            s = Scorer(name, src, t, start, end)
        except (SystemExit, StopIteration, KeyError):
            continue
        before, body, after = s.variant(t[start:end])
        pads = [(before + "".join(f"extern int Pad{i};\n" for i in range(n)), body, after) for n in range(limit)]
        with ThreadPoolExecutor(jobs) as pool:
            scores = list(pool.map(s.score, pads))
        shutil.rmtree(s.tmp, ignore_errors=True)
        hits[name] = {n for n, sc in enumerate(scores) if sc is not None and sc % 1000 == 0}
        print(f"{name}: {' '.join(map(str, sorted(hits[name]))) or '-'}", flush=True)
    order = [n for n in names if n in hits]
    best = [[0] * limit for _ in order]
    for j, name in enumerate(order):
        run = 0
        for c in range(limit):
            if j:
                run = max(run, best[j - 1][c])
            best[j][c] = run + (c in hits[name])
    c = max(range(limit), key=lambda c: best[-1][c]) if order else 0
    chosen = []
    for j in range(len(order) - 1, -1, -1):
        chosen.append(c)
        if j:
            c = max(range(c + 1), key=lambda k: best[j - 1][k])
    chosen.reverse()
    now = sum(0 in hits[n] for n in order)
    print(f"as written {now} of {len(order)} match; with this plan {best[-1][chosen[-1]] if order else 0}:")
    prev = 0
    for name, c in zip(order, chosen):
        mark = "ok" if c in hits[name] else "--"
        print(f"  {mark} {name}: {c}" + (f"  (+{c - prev} before it)" if c != prev else ""))
        prev = c


def declaration_runs(text):
    """Runs of adjacent top-level declaration lines (externs and
    prototypes, also inside extern "C" blocks), as (first, last) line
    indices into text.split("\\n")."""
    lines, runs, first = text.split("\n"), [], None
    depth, extern_block = 0, False
    for i, line in enumerate(lines):
        top = depth == 0 or (depth == 1 and extern_block)
        if top and TOP_DECL.match(line):
            first = i if first is None else first
        else:
            if first is not None and i - first >= 2:
                runs.append((first, i - 1))
            first = None
        if depth == 0:
            extern_block = line.startswith('extern "C" {')
        depth += line.count("{") - line.count("}")
    if first is not None and len(lines) - first >= 2:
        runs.append((first, len(lines) - 1))
    return runs


def file_order(variant, rng):
    """Moves one top-level declaration line before or after the function
    to another place in its run of declarations."""
    before, body, after = variant
    sides = [(k, part, declaration_runs(part)) for k, part in ((0, before), (1, after))]
    sides = [x for x in sides if x[2]]
    if not sides:
        return None
    k, part, runs = rng.choice(sides)
    a, b = rng.choice(runs)
    lines = part.split("\n")
    i, j = rng.randint(a, b), rng.randint(a, b)
    if i == j:
        return None
    lines.insert(j, lines.pop(i))
    part = "\n".join(lines)
    return (part, body, after) if k == 0 else (before, body, part)


GLOBAL_REWRITES = {"reorder_declarations", "shuffle_declarations", "file_order"}


def changed_lines(old, new):
    """The range of lines of `new` that differ from `old`, by common prefix
    and suffix."""
    a, b = old.split("\n"), new.split("\n")
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    s = 0
    while s < min(len(a), len(b)) - p and a[-1 - s] == b[-1 - s]:
        s += 1
    return p, len(b) - s


def classify(old, new):
    """The kinds of rewrite that turn body `old` into `new`, roughly, by
    looking at the lines that differ."""
    a, b = old.split("\n"), new.split("\n")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    kinds = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        gone, came = a[i1:i2], b[j1:j2]
        if any(l.strip() in {x.strip() for x in a} for l in came) and tag == "insert" or tag == "delete":
            kinds.add("move_statement")
        for x, y in zip(gone, came):
            if DECL.match(x) and DECL.match(y):
                kinds.add("reorder_declarations")
            elif sorted(re.findall(r"\w+|\S", x)) == sorted(re.findall(r"\w+|\S", y)):
                kinds.add("swap_logical" if "&&" in x or "||" in x else "swap_operands")
        text_gone, text_came = "\n".join(gone), "\n".join(came)
        for kind, pat in (("compound", r"[-+*&|^]= "), ("increment_form", r"\+\+|--|[-+]= 1;"),
                          ("condition_form", r"== 0|!= 0|\(!"), ("ternary_form", r" \? "),
                          ("loop_form", r"\b(?:for|while) \("), ("address_form", r"&\w+\["),
                          ("inline_return", r"\breturn\b"), ("default_case", r"default:"),
                          ("flip_if", r"\belse\b")):
            if len(re.findall(pat, text_gone)) != len(re.findall(pat, text_came)):
                kinds.add(kind)
    if set(a) == set(b) and a != b and not kinds:
        kinds.add("swap_statements")
    return kinds


def load_stats(name):
    """Tries and wins per rewrite: build/permute/_prior.json, which farm.py
    --learn writes, then this function's own NAME.stats.json."""
    import json
    stats = {}
    for path in (os.path.join(OUT, "_prior.json"), out_path(name, ".stats.json")):
        try:
            for k, (tries, wins) in json.load(open(path)).items():
                old = stats.get(k, [0, 0])
                stats[k] = [old[0] + tries, old[1] + wins]
        except (OSError, ValueError):
            pass
    return stats


def save_stats(name, stats, prior):
    import json
    own = {k: [v[0] - prior.get(k, [0, 0])[0], v[1] - prior.get(k, [0, 0])[1]] for k, v in stats.items()}
    os.makedirs(OUT, exist_ok=True)
    json.dump(own, open(out_path(name, ".stats.json"), "w"))


def weight(stats, kind):
    tries, wins = stats.get(kind, [0, 0])
    return 1 + 50 * wins / (tries + 50)


SWEEP_FORMS = ["condition_form", "increment_form", "ternary_form", "address_form", "compound", "inline_return",
               "flip_if", "swap_logical", "default_case", "loop_form", "move_statement", "swap_statements"]


def line_of(body, offset):
    return body.count("\n", 0, offset)


def sweep_dimensions(variant, hot, limit):
    """The neighbourhoods --sweep enumerates around `variant`, as
    (name, candidates): every order of the declarations, every place each
    top-level declaration before the function can move to in its run,
    every combination of swapped operands in the hot lines, and every
    single application of the other rewrites that touches them."""
    before, body, after = variant
    near = lambda lo, hi: not hot or any(lo <= n < max(hi, lo + 1) for n in hot)
    out = [("declaration orders", [(before, b, after) for b in declaration_orders(body, limit, random.Random(0))[1:]])]
    lines, moved = before.split("\n"), []
    for a, b in (declaration_runs(before)[-3:] if FILE_MODE[0] else []):
        for i in range(a, b + 1):
            for j in range(a, b + 1):
                if i != j:
                    copy = list(lines)
                    copy.insert(j, copy.pop(i))
                    moved.append(("\n".join(copy), body, after))
    out.append(("extern orders", moved[:limit]))
    hits = [m for m in operand_pattern().finditer(body) if near(line_of(body, m.start()), line_of(body, m.end()) + 1)]
    hits = hits[:12]
    combos = []
    for mask in range(1, 1 << len(hits)):
        text = body
        for k in reversed(range(len(hits))):
            if mask >> k & 1:
                m = hits[k]
                a, op, b = m.group(1), FLIPPED.get(m.group(2), m.group(2)), m.group(3)
                text = text[:m.start(1)] + f"{b} {op} {a}" + text[m.end(3):]
        combos.append((before, text, after))
    out.append((f"operand orders ({len(hits)} operators)", combos))
    forms, seen = [], {body}
    for name in SWEEP_FORMS:
        f = globals()[name]
        for seed in range(64):
            new = f(body, random.Random(seed))
            if new and new not in seen and near(*changed_lines(body, new)):
                seen.add(new)
                forms.append((before, new, after))
    out.append(("rewrites at the hot lines", forms))
    return out


def sweep(s, best, best_score, args, keep_best):
    """Beam search over sweep_dimensions until a whole pass finds nothing
    better: returns the best variant and score, and whether it got that
    far (`exhausted`) within the time."""
    beam, seen = [(best_score, best)], {"".join(best)}
    deadline = time.time() + args.t
    passes = 0
    with ThreadPoolExecutor(args.j) as pool:
        while best_score and time.time() < deadline:
            passes += 1
            better = False
            for score, variant in list(beam):
                hot = s.hot_lines(variant)
                hot |= {n + d for n in hot for d in (-1, 1)}
                for name, cands in sweep_dimensions(variant, hot, args.limit):
                    cands = [c for c in cands if "".join(c) not in seen]
                    seen.update("".join(c) for c in cands)
                    if not cands:
                        continue
                    print(f"  sweep {passes}: {name}, {len(cands)} variants from score {score}", flush=True)
                    done, step, mark = 0, args.j * 4, 0.25
                    for i in range(0, len(cands), step):
                        chunk = cands[i:i + step]
                        for c, sc in zip(chunk, pool.map(s.score, chunk)):
                            if sc is None:
                                continue
                            beam.append((sc, c))
                            if sc < best_score:
                                best, best_score, better = c, sc, True
                                print(f"  {len(seen):5d} tries: score {sc}", flush=True)
                                keep_best(best)
                        done += len(chunk)
                        if done / len(cands) >= mark and len(cands) >= 200:
                            print(f"    {done}/{len(cands)}", flush=True)
                            mark += 0.25
                        if not best_score or time.time() > deadline:
                            return best, best_score, False
                    beam = sorted(beam, key=lambda x: x[0])[:2]
            if not better:
                print(f"sweep exhausted after {passes} passes, {len(seen)} variants, best score {best_score}",
                      flush=True)
                return best, best_score, True
    return best, best_score, False


MUTATIONS = [swap_operands, swap_operands, reorder_declarations, shuffle_declarations, swap_statements,
             flip_if, swap_logical, compound, inline_return, condition_form, increment_form,
             ternary_form, loop_form, address_form, move_statement, default_case]


class Scorer:
    def __init__(self, name, src, text, start, end):
        self.name, self.src, self.text, self.start, self.end = name, src, text, start, end
        self.symbols = compare.read_symbols()
        row = next(r for r in compare_rows() if r["name"] == name)
        self.symbol_names = row["symbol"].split()
        self.symbol = self.symbol_names[0]
        self.addr, self.size = self.symbols[self.symbol]
        self.orig = coff.Image(os.path.join(ROOT, compare.EXE)).read(self.addr, self.size)
        self.orig_asm = compare.disasm(self.orig, self.addr)
        self.flags = build.file_flags(src, build.CFLAGS)
        self.tmp = tempfile.mkdtemp(prefix="permute", dir=os.path.join(ROOT, "build"))
        self.lock = threading.Lock()
        self.n = 0
        self.matched = None

    def variant(self, body):
        return (self.text[:self.start], body, self.text[self.end:])

    def compile(self, variant, extra=()):
        """Compiles `variant` alone; the object's path, or None (see
        self.why). The caller removes the object."""
        with self.lock:
            self.n += 1
            k = self.n
        rel = os.path.relpath(self.src, "src")
        conv = os.path.join(build.SJIS, os.path.dirname(rel), f"_perm{os.getpid()}_{k}_{os.path.basename(rel)}")
        obj = os.path.join(self.tmp, f"{k}.obj")
        with open(conv, "wb") as f:
            f.write(build.to_sjis("".join(variant)))
        try:
            code, out = build.run_tool("cl", "/c", *self.flags, *extra, "/I" + build.win(build.SJIS),
                                     "/Fo" + build.win(obj), build.win(conv))
        finally:
            os.remove(conv)
        if code or not os.path.exists(obj):
            self.why = "cl failed:\n" + "\n".join(l for l in out.splitlines() if "error" in l)[:1500]
            if os.path.exists(obj):
                os.remove(obj)
            return None
        return obj

    def results(self, obj):
        with COMPARE_LOCK:
            saved = build.sources
            try:
                build.sources = lambda *a, **kw: [(self.src, obj)]
                return list(compare.results(self.symbols))
            finally:
                build.sources = saved

    def ours(self, res):
        """Our instructions, with the original's bytes where only
        relocations differ."""
        _, same, code_bytes, mask, orig, diffs = res
        shown = bytes(b if m and i < len(orig) else a
                      for i, (a, m, b) in enumerate(zip(code_bytes, mask, orig + bytes(len(code_bytes)))))
        return compare.disasm(shown, self.addr)

    def score(self, variant):
        if isinstance(variant, str):
            variant = self.variant(variant)
        obj = self.compile(variant)
        if obj is None:
            return None
        try:
            every = self.results(obj)
        finally:
            os.remove(obj)
        res = [r for r in every if r[0] in self.symbol_names]
        matched = {r[0] for r in every if r[1] and r[0] not in self.symbol_names}
        if self.matched is None:
            self.matched = matched
        broken = len(self.matched - matched)
        if not res:
            self.why = (f"compiled, but the object defines none of {' '.join(self.symbol_names)} (symbols.csv's "
                        f"symbols for {self.name}): check the function's linkage, extern \"C\" or C++")
            return None
        _, same, code_bytes, mask, orig, diffs = res[0]
        if same:
            return 1000 * broken
        ours = self.ours(res[0])
        sm = difflib.SequenceMatcher(None, ours, self.orig_asm, autojunk=False)
        return (len(ours) + len(self.orig_asm) - 2 * sum(b.size for b in sm.get_matching_blocks()) + len(diffs)
                + 1000 * broken)


    def hot_lines(self, variant):
        """The lines of `variant`'s body, counted from 0, that compile to
        instructions differing from the original's, from the line
        numbers /Zd records. /Zd doesn't change the code, but its .bf and
        .lf symbols confuse the sizes coff.py reads, so the code comes
        from a plain compile."""
        obj = self.compile(variant)
        if obj is None:
            return set()
        try:
            res = [r for r in self.results(obj) if r[0] in self.symbol_names]
        finally:
            os.remove(obj)
        obj = self.compile(variant, ["/Zd"])
        if obj is None:
            return set()
        try:
            table = coff_lines(obj, self.symbol_names)
        finally:
            os.remove(obj)
        if not res or not table or res[0][1]:
            return set()
        ours = self.ours(res[0])
        sm = difflib.SequenceMatcher(None, ours, self.orig_asm, autojunk=False)
        kept = set()
        for b in sm.get_matching_blocks():
            kept.update(range(b.a, b.a + b.size))
        offset, bad = 0, []
        for i, line in enumerate(ours):
            if i not in kept:
                bad.append(offset)
            offset += len(line.split("\t", 1)[0].split())
        first = variant[0].count("\n")
        lines = set()
        for at in bad:
            line = max((n for va, n in table if va <= at), default=None)
            if line is not None:
                lines.add(line - 1 - first)
        return {n for n in lines if n >= 0}


def coff_lines(path, symbols):
    """(offset, source line) pairs of the first of `symbols` in the COFF
    object at `path`, from its line number table."""
    import struct
    d = open(path, "rb").read()
    _, nsec, _, symptr, nsym, opt, _ = struct.unpack_from("<HHIIIHH", d, 0)
    strtab = symptr + nsym * 18
    names, i, bf = {}, 0, {}
    while i < nsym:
        o = symptr + i * 18
        raw = d[o:o + 8]
        if raw[:4] == b"\0\0\0\0":
            at = strtab + struct.unpack_from("<I", raw, 4)[0]
            name = d[at:d.index(b"\0", at)].decode("latin-1")
        else:
            name = raw.rstrip(b"\0").decode("latin-1")
        sec, aux = struct.unpack_from("<h", d, o + 12)[0], d[o + 17]
        names.setdefault(name, (i, sec))
        if name == ".bf" and aux:
            bf[sec] = struct.unpack_from("<H", d, o + 18 + 4)[0]
        i += 1 + aux
    hit = next((names[s] for s in symbols if s in names), None)
    if not hit:
        return []
    index, sec = hit
    o = 20 + opt + (sec - 1) * 40
    ptr, = struct.unpack_from("<I", d, o + 28)
    count, = struct.unpack_from("<H", d, o + 34)
    base = bf.get(sec, 0)
    out = []
    for k in range(count):
        va, line = struct.unpack_from("<IH", d, ptr + 6 * k)
        if line:
            out.append((va, base + line))
    return sorted(out)


# A local declaration line: indent, type, names. A line with an
# initializer (`Data *data = (Data *)p;`) moves as a whole.
DECL = re.compile(r"^(\s+)((?:const |unsigned |signed |static |register |struct |enum |class )*[A-Za-z_]\w*[\s\*]+)"
                  r"(\**[A-Za-z_]\w*[^;{}]*);$")


def declared_names(names):
    """The names a declaration line declares, which may be shuffled; an
    initialized one is a single item."""
    if "=" in names or "(" in names:
        return [names.strip()]
    return [n.strip() for n in names.split(",")]


def declaration_orders(body, limit, rng):
    """Orders of the top declaration lines and of the names in each, the
    original first: all of them when there are at most `limit`, otherwise
    `limit` drawn at random. Memory stays proportional to `limit`."""
    import itertools, math
    lines = body.split("\n")
    top = top_lines_of(lines, DECL)
    if not top:
        return []
    parts = []
    for i in top:
        m = DECL.match(lines[i])
        parts.append((m.group(1), m.group(2).rstrip(), declared_names(m.group(3))))

    def render(order, names):
        block = [f"{parts[i][0]}{parts[i][1]}{'' if parts[i][1].endswith('*') else ' '}{', '.join(ns)};"
                 for i, ns in zip(order, names)]
        return "\n".join(lines[:top[0]] + block + lines[top[-1] + 1:])

    total = math.factorial(len(parts)) * math.prod(math.factorial(len(p[2])) for p in parts)
    if total <= limit:
        out = []
        for order in itertools.permutations(range(len(parts))):
            for names in itertools.product(*[itertools.permutations(parts[i][2]) for i in order]):
                out.append(render(order, names))
        return out
    out, seen = [body], set()
    tries = 0
    while len(out) < limit and tries < limit * 20:
        tries += 1
        order = list(range(len(parts)))
        rng.shuffle(order)
        names = []
        for i in order:
            ns = list(parts[i][2])
            rng.shuffle(ns)
            names.append(tuple(ns))
        key = (tuple(order), tuple(names))
        if key in seen:
            continue
        seen.add(key)
        out.append(render(order, names))
    return out


def compare_rows():
    import csv
    return list(csv.DictReader(open("config/symbols.csv")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("-j", type=int, default=max(2, build.JOBS))
    ap.add_argument("-t", type=float, default=600)
    ap.add_argument("-s", type=int, default=None)
    ap.add_argument("--decls", action="store_true", help="search declaration orders exhaustively")
    ap.add_argument("--limit", type=int, default=3000, help="--decls: most candidates to try")
    ap.add_argument("--start", help="a variant to search from, such as an earlier run's best")
    ap.add_argument("--sweep", action="store_true",
                    help="enumerate the neighbourhoods of the diff's lines, beam search, until nothing helps")
    ap.add_argument("--pad", action="store_true",
                    help="look for a shift of the symbol IDs, then a declaration move that makes it")
    ap.add_argument("--plan", action="store_true",
                    help="plan how many declarations to add before each function in the file")
    ap.add_argument("--file", action="store_true",
                    help="also reorder the file's top-level declarations, keeping its other functions matching")
    args = ap.parse_args()
    FILE_MODE[0] = args.file or args.pad
    build.keep_wineserver()
    build.compile_all()
    src, text, start, end = find_function(args.name)
    if args.plan:
        plan(src, text, args.limit if args.limit != 3000 else 64, args.j)
        return 0
    s = Scorer(args.name, src, text, start, end)
    base = s.variant(text[start:end])
    FILE_MODE[1] = (base[0], base[2])
    first = s.score(base)
    if first is None:
        sys.exit("the function doesn't build as it is: " + getattr(s, "why", "unknown reason"))
    print(f"{args.name}: {src}, score {first} as written", flush=True)
    if first == 0 and not args.pad:
        print("already matches")
        return 0
    rng = random.Random(args.s)
    best, best_score = base, first
    if args.start and os.path.exists(args.start):
        seed = s.variant(open(args.start, encoding="utf-8").read())
        whole = os.path.splitext(args.start)[0] + ".file"
        if args.file and os.path.exists(whole):
            full = open(whole, encoding="utf-8").read()
            at = full.find(seed[1])
            if at >= 0:
                seed = (full[:at], seed[1], full[at + len(seed[1]):])
        score = s.score(seed)
        if score is not None and score < best_score:
            best, best_score = seed, score
            print(f"starting from {args.start}, score {score}", flush=True)
    cur, cur_score = best, best_score
    deadline = time.time() + args.t if best_score else 0
    tried = 0

    if args.pad:
        found = pad(s, best, args.limit if args.limit != 3000 else 32, args.j)
        if found:
            best, best_score = found, 0
        keep(args.name, best)
        deadline = 0
    elif args.sweep and best_score:
        best, best_score, _ = sweep(s, best, best_score, args, lambda v: keep(args.name, v))
        tried = 0
        deadline = 0
    elif args.decls and best_score:
        cands = [(best[0], b, best[2]) for b in declaration_orders(best[1], args.limit, rng)[1:]]
        print(f"{len(cands)} declaration orders to try", flush=True)
        step = args.j * 4
        with ThreadPoolExecutor(args.j) as pool:
            for i in range(0, len(cands), step):
                chunk = cands[i:i + step]
                for body, sc in zip(chunk, pool.map(s.score, chunk)):
                    tried += 1
                    if sc is not None and sc < best_score:
                        best, best_score = body, sc
                        print(f"  {tried:5d} tries: score {sc}", flush=True)
                        keep(args.name, best)
                if best_score == 0:
                    break
        deadline = 0

    stats = load_stats(args.name)
    prior = load_stats("\0")
    hot = set()

    def focus(variant):
        nonlocal hot
        hot = s.hot_lines(variant)
        hot |= {n + d for n in hot for d in (-1, 1)}

    def pick(r):
        kinds = sorted(set(MUTATIONS), key=lambda f: f.__name__)
        return r.choices(kinds, [weight(stats, f.__name__) for f in kinds])[0]

    def candidate(seed, r):
        before, body, after = seed
        used = []
        for _ in range(r.choice([1, 1, 1, 2, 3])):
            if args.file and r.random() < 0.3 * weight(stats, "file_order") / weight(stats, "swap_operands"):
                new = file_order((before, body, after), r)
                if new:
                    before, body, after = new
                    used.append("file_order")
                continue
            for _ in range(8):
                f = pick(r)
                new = f(body, r)
                if not new:
                    continue
                lo, hi = changed_lines(body, new)
                if not hot or f.__name__ in GLOBAL_REWRITES or r.random() < 0.25 \
                        or any(lo <= n < max(hi, lo + 1) for n in hot):
                    body = new
                    used.append(f.__name__)
                    break
        return (before, body, after), used

    if deadline:
        focus(best)
    with ThreadPoolExecutor(args.j) as pool:
        while time.time() < deadline and best_score:
            made = [candidate(cur, random.Random(rng.random())) for _ in range(args.j)]
            bodies = [m[0] for m in made]
            for (body, used), sc in zip(made, pool.map(s.score, bodies)):
                tried += 1
                for kind in used:
                    stats.setdefault(kind, [0, 0])[0] += 1
                if sc is None:
                    continue
                if sc < cur_score:
                    for kind in used:
                        stats[kind][1] += 1
                if sc < best_score:
                    best, best_score = body, sc
                    print(f"  {tried:5d} tries: score {sc}", flush=True)
                    keep(args.name, best)
                    save_stats(args.name, stats, prior)
                    if best_score:
                        focus(best)
                if sc <= cur_score or rng.random() < 0.05:
                    cur, cur_score = body, sc
            if rng.random() < 0.02:
                cur, cur_score = best, best_score
    if tried:
        save_stats(args.name, stats, prior)
    shutil.rmtree(s.tmp, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    keep(args.name, best)
    out = out_path(args.name, ".c")
    print(f"{tried} tries, best score {best_score}{' (MATCH)' if best_score == 0 else ''}; written to {out}")
    if best != base:
        sys.stdout.writelines(difflib.unified_diff("".join(base).splitlines(True), "".join(best).splitlines(True),
                                                   "as written", "best variant", n=1))
    return 0 if best_score == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
