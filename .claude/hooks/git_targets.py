"""For check-issue.sh: which directories does a Bash command `git commit`/`git push` in?

Reads the command on stdin, prints one directory per git commit/push (relative ones are
resolved against argv[1], the session cwd). Only `git` in command position counts: quoted text
(`grep "git push" GIT.md`) is an argument, not a command. Understands `cd <dir>` (with spaces,
quoted), `git -C <dir>`, other global options (`-c k=v`, `--no-pager`, …), env assignments and
`&&`, `||`, `;`, `|`, `(`. A heredoc body is data. Branch deletion (`push --delete`, `push -d`,
`push origin :branch`) is cleanup, not work, and is skipped. Unparseable input -> the cwd (fail
closed: the check still runs).
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import sys

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


def _join(base: str, path: str) -> str:
    """Paths as the calling shell sees them. Native Windows Python must not reinterpret Git Bash
    paths (/tmp/x, /c/Users/...): join and normalize with forward slashes."""
    path = os.path.expanduser(path)
    if path.startswith("/") or _DRIVE.match(path):
        return path
    return base.replace("\\", "/").rstrip("/") + "/" + path


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))
GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def commands(text: str) -> list[list[str]]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    out: list[list[str]] = [[]]
    for tok in lex:
        if tok in SEPARATORS or set(tok) <= set(";&|()"):
            out.append([])
        else:
            out[-1].append(tok)
    return [c for c in out if c]


def targets(text: str, cwd: str) -> list[str]:
    cmds: list[list[str]] = []
    # Each line is its own command list (a newline separates commands; a comment ends at it);
    # a trailing backslash continues the line; a heredoc body is data.
    for line in text.replace("\\\n", " ").splitlines():
        for tail in ("", '"', "'", "'\""):  # a quote opened here closes on a later line
            try:
                parsed = commands(line + tail)
                break
            except ValueError:
                continue
        else:
            return [cwd]
        cmds += parsed
        if any(t.startswith("<<") for c in parsed for t in c):  # a real heredoc, not "<<" in quotes
            break
    here, found = cwd, []
    for c in cmds:
        while c and "=" in c[0] and not c[0].startswith("-") and c[0].split("=")[0].isidentifier():
            c = c[1:]  # FOO=bar git commit
        if not c:
            continue
        if c[0] == "cd" and len(c) > 1:
            here = _join(here, c[1])
            continue
        if os.path.basename(c[0]) != "git":
            continue
        i, where = 1, here
        while i < len(c) and c[i].startswith("-"):
            if c[i] == "-C" and i + 1 < len(c):
                where = _join(where, c[i + 1])
            i += 2 if c[i] in GLOBAL_WITH_ARG else 1
        if i >= len(c) or c[i] not in ("commit", "push"):
            continue
        rest = c[i + 1:]
        if c[i] == "push" and ("--delete" in rest or "-d" in rest
                               or any(a.startswith(":") for a in rest if not a.startswith("-"))):
            continue
        found.append(_norm(where))
    return found


if __name__ == "__main__":
    print("\n".join(targets(sys.stdin.read(), sys.argv[1] if len(sys.argv) > 1 else ".")))
