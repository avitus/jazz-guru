"""``patch`` — multi-strategy find-and-replace for workspace files.

A safer, more forgiving replacement for ``code_edit``. Strategies are tried
in order; the first match wins. Returns a unified diff and, for Python
files, runs ``ast.parse`` post-edit so syntactically broken patches are
caught immediately instead of failing later at runtime.

Strategies
----------
1. **exact**            — ``str.find`` on the file content.
2. **line-trimmed**     — match a window of lines whose ``str.strip()`` form
   equals the ``find`` string's. Tolerates indentation differences.
3. **fuzzy (line-wise)**— slide a same-line-count window across the file,
   compute ``difflib.SequenceMatcher(None, find, window).ratio()``, and
   accept the best window if the ratio meets ``min_ratio`` (default 0.85).

If ``find`` matches via strategy 1 more than once, the call is rejected
unless ``change_all=true``. Strategies 2 and 3 always operate on a single
best window.
"""
from __future__ import annotations

import ast
import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace


class PatchInput(BaseModel):
    path: str = Field(..., description="Target file path (relative to session workspace).")
    find: str = Field(..., description="Text to locate. Tried exact, then line-trimmed, then fuzzy.")
    replace: str = Field(..., description="Replacement text.")
    change_all: bool = Field(
        default=False,
        description="Only meaningful for exact matches. When false, multiple exact matches are rejected.",
    )
    min_ratio: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Minimum SequenceMatcher ratio for the fuzzy strategy. 1.0 disables fuzzy.",
    )
    syntax_check: bool = Field(
        default=True,
        description="For *.py files, run ast.parse on the result. On failure the file is reverted.",
    )


def _do_exact(text: str, find: str, replace: str, change_all: bool) -> tuple[str, int, str] | None:
    count = text.count(find)
    if count == 0:
        return None
    if count > 1 and not change_all:
        return None
    new = text.replace(find, replace) if change_all else text.replace(find, replace, 1)
    return new, count if change_all else 1, "exact"


class _AmbiguousMatch(Exception):
    """A strategy found multiple matches and refuses to guess which one to patch."""

    def __init__(self, strategy: str, count: int) -> None:
        super().__init__(f"{strategy} matched {count} times")
        self.strategy = strategy
        self.count = count


def _do_line_trimmed(text: str, find: str, replace: str) -> tuple[str, int, str] | None:
    text_lines = text.splitlines(keepends=True)
    find_lines = find.splitlines()
    if not find_lines:
        return None
    target = [ln.strip() for ln in find_lines]
    n = len(target)
    if n > len(text_lines):
        return None
    matches: list[int] = []
    for start in range(len(text_lines) - n + 1):
        window = [ln.strip() for ln in text_lines[start : start + n]]
        # Tolerate trailing blank lines that splitlines might have lost.
        if window == target:
            matches.append(start)
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        # Multiple matches via line-trimmed: do NOT fall through to fuzzy --
        # an ambiguous line match is much more reliable than a fuzzy guess
        # against the wrong block.
        raise _AmbiguousMatch("line_trimmed", len(matches))
    start = matches[0]
    # Preserve the indentation of the first replaced line on every replacement line.
    indent = _leading_ws(text_lines[start])
    repl_lines = _apply_indent(replace, indent)
    # Match the trailing-newline behavior of the window we're replacing.
    repl_lines = _align_trailing_newline(repl_lines, text_lines[start + n - 1])
    new_text = (
        "".join(text_lines[:start]) + repl_lines + "".join(text_lines[start + n :])
    )
    return new_text, 1, "line_trimmed"


def _do_fuzzy(
    text: str, find: str, replace: str, min_ratio: float
) -> tuple[str, int, str, float] | None:
    if min_ratio >= 1.0:
        return None
    text_lines = text.splitlines(keepends=True)
    find_lines = find.splitlines(keepends=True) or [find]
    n = len(find_lines)
    if n > len(text_lines):
        return None
    best_ratio = 0.0
    best_starts: list[int] = []
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq1(find)
    for start in range(len(text_lines) - n + 1):
        window = "".join(text_lines[start : start + n])
        matcher.set_seq2(window)
        r = matcher.ratio()
        if r > best_ratio:
            best_ratio = r
            best_starts = [start]
        elif r == best_ratio and best_ratio > 0:
            best_starts.append(start)
    # Fail closed (like exact / line-trimmed strategies) when the score is
    # below the threshold OR multiple equally-good windows exist — silently
    # editing the first tie would risk corrupting the wrong block.
    if best_ratio < min_ratio or len(best_starts) != 1:
        return None
    best_start = best_starts[0]
    indent = _leading_ws(text_lines[best_start])
    repl_lines = _apply_indent(replace, indent)
    repl_lines = _align_trailing_newline(repl_lines, text_lines[best_start + n - 1])
    new_text = (
        "".join(text_lines[:best_start])
        + repl_lines
        + "".join(text_lines[best_start + n :])
    )
    return new_text, 1, "fuzzy", best_ratio


def _leading_ws(line: str) -> str:
    stripped = line.lstrip()
    return line[: len(line) - len(stripped)]


def _align_trailing_newline(repl: str, window_last_line: str) -> str:
    """Match the trailing-newline behavior of ``window_last_line`` in ``repl``.

    The strategies operate on ``splitlines(keepends=True)`` windows, so the
    last line of the matched window may or may not carry a ``\\n``. The user's
    replacement string usually doesn't worry about that, so we normalize.
    """
    window_has_nl = window_last_line.endswith("\n")
    repl_has_nl = repl.endswith("\n")
    if window_has_nl and not repl_has_nl:
        return repl + "\n"
    if not window_has_nl and repl_has_nl:
        return repl.rstrip("\n")
    return repl


def _apply_indent(text: str, indent: str) -> str:
    """Re-indent ``text`` so each non-empty line carries at least ``indent``.

    If ``text`` already has its own leading indentation, we don't double it up:
    we left-strip the common leading-whitespace prefix and prepend ``indent``
    to each line. Empty lines are left untouched.
    """
    if not indent:
        return text
    lines = text.splitlines(keepends=True)
    # Find the minimum leading-whitespace across non-empty lines.
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return text
    min_lead = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
    out: list[str] = []
    for ln in lines:
        if not ln.strip():
            out.append(ln)
            continue
        out.append(indent + ln[min_lead:])
    return "".join(out)


def _make_diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=3,
        )
    )


def _python_syntax_ok(text: str) -> tuple[bool, str | None]:
    try:
        ast.parse(text)
        return True, None
    except SyntaxError as e:
        return False, f"{e.msg} at line {e.lineno}, col {e.offset}"


async def _patch_file(
    path: str,
    find: str,
    replace: str,
    *,
    change_all: bool = False,
    min_ratio: float = 0.85,
    syntax_check: bool = True,
) -> dict[str, object]:
    """Shared implementation used by both ``patch`` and the back-compat ``code_edit``."""
    p = resolve_in_workspace(path, current().session_id)
    if not find:
        return {"ok": False, "edited": False, "reason": "find must not be empty"}
    if not p.exists():
        return {"ok": False, "edited": False, "reason": "missing file", "path": str(p)}
    text = p.read_text(encoding="utf-8")
    if find == replace:
        return {
            "ok": False,
            "edited": False,
            "reason": "find and replace are identical (no-op)",
            "path": str(p),
        }

    strategy_used: str
    replacements: int
    fuzzy_ratio: float | None = None

    exact = _do_exact(text, find, replace, change_all)
    if exact is not None:
        new_text, replacements, strategy_used = exact
    else:
        # If exact found multiple matches but change_all is false, surface that
        # specifically instead of silently falling back to fuzzy strategies.
        exact_count = text.count(find)
        if exact_count > 1 and not change_all:
            return {
                "ok": False,
                "edited": False,
                "reason": (
                    f"find matches {exact_count} times via exact strategy; "
                    "set change_all=true or expand find for uniqueness"
                ),
                "path": str(p),
                "strategy": "exact",
                "matches": exact_count,
            }
        try:
            line_trim = _do_line_trimmed(text, find, replace)
        except _AmbiguousMatch as ambig:
            return {
                "ok": False,
                "edited": False,
                "reason": (
                    f"find matches {ambig.count} times via {ambig.strategy} "
                    "strategy; expand find for uniqueness"
                ),
                "path": str(p),
                "strategy": ambig.strategy,
                "matches": ambig.count,
            }
        if line_trim is not None:
            new_text, replacements, strategy_used = line_trim
        else:
            fuzzy = _do_fuzzy(text, find, replace, min_ratio)
            if fuzzy is None:
                return {
                    "ok": False,
                    "edited": False,
                    "reason": "find not located by any strategy",
                    "path": str(p),
                    "strategies_tried": ["exact", "line_trimmed", "fuzzy"],
                }
            new_text, replacements, strategy_used, fuzzy_ratio = fuzzy

    diff = _make_diff(p, text, new_text)

    if syntax_check and p.suffix == ".py":
        ok, err = _python_syntax_ok(new_text)
        if not ok:
            return {
                "ok": False,
                "edited": False,
                "reason": f"post-edit syntax error: {err}",
                "path": str(p),
                "strategy": strategy_used,
                "diff": diff,
            }

    p.write_text(new_text, encoding="utf-8")
    out: dict[str, object] = {
        "ok": True,
        "edited": True,
        "path": str(p),
        "strategy": strategy_used,
        "replacements": replacements,
        "bytes": len(new_text.encode("utf-8")),
        "diff": diff,
    }
    if fuzzy_ratio is not None:
        out["fuzzy_ratio"] = round(fuzzy_ratio, 3)
    return out


@registry.register(
    "patch",
    description=(
        "Multi-strategy find-and-replace edit on a workspace file. Tries exact "
        "match, then line-trimmed (tolerant of indentation), then fuzzy "
        "(SequenceMatcher ratio >= min_ratio, default 0.85). For *.py files "
        "the result is parsed with ast.parse and the edit is rejected if "
        "syntax breaks. Returns a unified diff."
    ),
    input_model=PatchInput,
    tags=("code",),
)
async def patch(
    path: str,
    find: str,
    replace: str,
    change_all: bool = False,
    min_ratio: float = 0.85,
    syntax_check: bool = True,
) -> dict[str, object]:
    return await _patch_file(
        path,
        find,
        replace,
        change_all=change_all,
        min_ratio=min_ratio,
        syntax_check=syntax_check,
    )
