"""What one bash command EXECUTES, as opposed to what it merely mentions (devloop + usage share it).

    sites(command) -> [{"path", "kind", "argv", "loop"}]
      kind: launch | help | read | import
    sql_strings(command) -> [raw SQL text]

Launch sites, not launch counts: a helper started inside a shell or Python loop is one site with
`loop=True`. Recognised: `python[3] x.py`, `uv run x.py`, `./x.py` and `/abs/x.py` (after launchers,
flags, `VAR=x`); Python code (heredoc fed to python or written to a `.py` file, `python -c`) that
passes a `.py` path to subprocess.run/call/check_output/Popen/os.system, or imports a module
(`import x` / `from x import`, kind=import, path = module name). `cat|sed|head|… x.py` is a read;
`x.py --help`/`-h` is help. A `cat <<EOF` body that is not Python is printed, never run. Stdlib only.
"""

from __future__ import annotations

import ast
import re
import shlex
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)  # agent heredocs: invalid escapes are not our signal

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "$", "$(", "`", "\n"}
LAUNCHERS = {"python", "python3", "uv", "run", "timeout", "env", "bash", "sh", "exec", "nice", "time",
             "do", "then", "else", "xargs"}
READERS = {"cat", "sed", "head", "tail", "rg", "grep", "ls", "wc", "nl", "less", "awk", "diff", "stat"}
SPAWNS = {"run", "call", "check_call", "check_output", "Popen", "system", "getoutput", "getstatusoutput"}
HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
LOOP = re.compile(r"(^|[;&|\s(])(for|while|until)\s.+?(;|\n)\s*do\b", re.S)
SQL_START = re.compile(r"^\s*(select|with)\b", re.I | re.M)
PY_FILE = re.compile(r"""['"]([^'"\s]+\.py)['"]""")


def is_prestep(event: dict) -> bool:
    seq = event.get("seq")
    return (bool(event.get("grounding")) or str(event.get("disp") or "").startswith("P")
            or (isinstance(seq, int) and seq < 0))


def is_main_bash(event: dict) -> bool:
    return (event.get("tool") == "bash" and not is_prestep(event)
            and not str(event.get("disp") or "").startswith("C"))


def tokens(line: str) -> list[str]:
    line = re.sub(r"(^|\s)\d+(?=[<>])", r"\1", line)  # `2>&1`: drop the fd number, keep `>&1`
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:  # unbalanced quotes (partial lines)
        return re.sub(r"([;|&()])", r" \1 ", line).split()


def split_heredocs(command: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(shell lines without heredoc bodies, [(opener line, body)])."""
    lines, bodies, pending, body = [], [], [], []
    for line in command.splitlines():
        if pending:
            strip_tabs, word, opener = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                bodies.append((opener, "\n".join(body)))
                pending.pop(0)
                body = []
            else:
                body.append(line)
            continue
        lines.append(line)
        pending = [(bool(m.group(1)), m.group(3), line) for m in HEREDOC.finditer(line)]
    if pending:  # unterminated heredoc: keep what we saw
        bodies.append((pending[0][2], "\n".join(body)))
    return lines, bodies


def segments(lines: list[str]):
    for line in lines:
        seg: list[str] = []
        for tok in tokens(line):
            if tok in SEPARATORS or (tok and set(tok) <= set(";|&()$`")):
                if seg:
                    yield seg
                seg = []
            else:
                seg.append(tok)
        if seg:
            yield seg


def _argv(rest: list[str]) -> list[str]:
    out = []
    for t in rest:
        if t[:1] in ("<", ">"):
            break
        out.append(t)
    return out


def _shell_sites(lines: list[str], loop: bool) -> tuple[list[dict], list[str]]:
    """Sites in shell segments, plus `python -c` code strings."""
    found, code = [], []
    for seg in segments(lines):
        first = next((t for t in seg if not ASSIGNMENT.match(t)), "")
        if first.rsplit("/", 1)[-1] in READERS:
            found += [{"path": t, "kind": "read", "argv": [], "loop": loop}
                      for prev, t in zip(seg, seg[1:]) if t.endswith(".py") and prev not in (">", ">>")]
            continue
        for i, tok in enumerate(seg):
            before = seg[:i]
            if before and before[-1] == "-c":
                if tok and all(t in LAUNCHERS or t.startswith("python3.") or t.startswith("-") for t in before):
                    code.append(tok)
                break
            if not tok.endswith(".py"):
                continue
            if ASSIGNMENT.match(tok) and all(ASSIGNMENT.match(t) for t in before):
                break  # `HELPER=/path/x.py` only assigns a variable
            if all(t in LAUNCHERS or t.startswith("python3.") or ASSIGNMENT.match(t) or t.isdigit()
                   or (t.startswith("-") and t != "-c") for t in before):
                argv = _argv(seg[i + 1:])
                kind = "help" if {"--help", "-h"} & set(argv) else "launch"
                found.append({"path": tok, "kind": kind, "argv": argv, "loop": loop})
            break
    return found, code


def _python_sites(code: str) -> list[dict]:
    """subprocess/os.system launches and imports inside Python source; regex fallback."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        loop = bool(re.search(r"^\s*(for|while)\b|\bmap\(", code, re.M))
        out = [{"path": p, "kind": "launch", "argv": [], "loop": loop}
               for m in re.finditer(r"\b(?:subprocess\.\w+|os\.system|Popen)\(([^)]*)", code)
               for p in PY_FILE.findall(m.group(1))]
        out += [{"path": m.group(1) or m.group(2), "kind": "import", "argv": [], "loop": False}
                for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", code, re.M)]
        return out
    out = []

    def walk(node, loop):
        for child in ast.iter_child_nodes(node):
            inner = loop or isinstance(child, (ast.For, ast.While, ast.comprehension, ast.FunctionDef,
                                               ast.Lambda, ast.ListComp, ast.GeneratorExp))
            if isinstance(child, ast.Call):
                name = getattr(child.func, "attr", None) or getattr(child.func, "id", None)
                if name in SPAWNS and child.args:
                    arg = child.args[0]
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        parts = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else "?"
                                 for e in arg.elts]
                        for i, part in enumerate(parts):
                            if not part.endswith(".py"):
                                continue
                            argv, before = parts[i + 1:], parts[:i]
                            if before and before[0].rsplit("/", 1)[-1] in READERS:
                                kind = "read"  # subprocess.run(['cat', 'x.py']) inspects, never runs
                            elif all(t in LAUNCHERS or t.startswith("python") or t == "?" or t.startswith("-")
                                     for t in before):  # "?" = sys.executable or another non-literal
                                kind = "help" if {"--help", "-h"} & set(argv) else "launch"
                            else:
                                break
                            out.append({"path": part, "kind": kind, "argv": argv if kind != "read" else [],
                                        "loop": inner})
                            break
                    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        out.extend({**s, "loop": s["loop"] or inner} for s in sites(arg.value)
                                   if s["kind"] in ("launch", "help"))
            elif isinstance(child, ast.Import):
                out.extend({"path": a.name, "kind": "import", "argv": [], "loop": False} for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module and not child.level:
                out.append({"path": child.module, "kind": "import", "argv": [], "loop": False})
            walk(child, inner)

    walk(tree, False)
    return out


def _is_python_heredoc(opener: str) -> bool:
    return bool(re.search(r"\bpython[\d.]*\b|>\s*\S+\.py\b", opener))


def _parts(command: str) -> tuple[list[dict], list[str], list[str]]:
    """(shell sites, Python sources: heredocs fed to python or written to .py + `python -c`, shell lines)."""
    lines, bodies = split_heredocs(command or "")
    found, code = _shell_sites(lines, bool(LOOP.search("\n".join(lines))))
    return found, [body for opener, body in bodies if _is_python_heredoc(opener)] + code, lines


def sites(command: str) -> list[dict]:
    found, code, _ = _parts(command)
    return found + [site for source in code for site in _python_sites(source)]


def helper_calls(command: str, helper: str) -> list[list[str]]:
    """argv after the helper path for every launch of `helper` in `command` (devloop report)."""
    return [s["argv"] for s in sites(command) if s["kind"] in ("launch", "help") and helper in s["path"]]


def sql_strings(command: str) -> list[str]:
    """SQL text the command sends: Python string literals starting SELECT/WITH, `psql -c`, psql heredocs."""
    out, (_, code_parts, lines) = [], _parts(command)
    for code in code_parts:
        try:
            nodes = list(ast.walk(ast.parse(code)))
            fparts = {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
            consts = [n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and id(n) not in fparts]
            consts += ["".join(v.value if isinstance(v, ast.Constant) else "?" for v in n.values)
                       for n in nodes if isinstance(n, ast.JoinedStr)]
        except (SyntaxError, ValueError):
            consts = [m.group(2) for m in re.finditer(r"(\"\"\"|'''|\"|')(.*?)\1", code, re.S)]
        out += [c for c in consts if SQL_START.match(c) and re.search(r"\bfrom\b", c, re.I)]
    out += [stmt.strip() for opener, body in split_heredocs(command or "")[1] if re.search(r"\bpsql\b", opener)
            for stmt in body.split(";") if SQL_START.match(stmt)]
    for seg in segments(lines):
        if seg and seg[0] == "psql":
            out += [seg[i + 1] for i, t in enumerate(seg[:-1]) if t in ("-c", "--command")]
    return out


def normalise_sql(sql: str) -> str:
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).lower()
    s = re.sub(r"'(?:[^']|'')*'", "?", s)
    s = re.sub(r"%\(\w+\)s|%s|\$\d+|(?<![:\w]):\w+|\b[0-9a-f]{8}-[0-9a-f-]{27}\b|\b\d+(\.\d+)?\b", "?", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(";").strip()
    return re.sub(r"\(\s*\?(\s*,\s*\?)+\s*\)", "(?)", s)


def sql_tables(normalised: str) -> list[str]:
    ctes = set(re.findall(r"(?:with|,)\s*(\w+)\s+as\s*\(", normalised))
    names = re.findall(r"\b(?:from|join)\s+([a-z_][\w.\"]*)", normalised)
    return sorted({n.strip('"') for n in names} - ctes - {"lateral", "unnest", "generate_series"})
