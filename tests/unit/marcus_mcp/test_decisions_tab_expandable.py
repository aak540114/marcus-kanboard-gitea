"""
Regression guards for the expandable "Silent Decisions" cards on the
/project-description page (project_description_page in
src/marcus_mcp/server.py).

Each decision note is now "<one-line summary>" + a blank line + labeled
sections (see build_tiered_instructions Layer 1.25 in
src/marcus_mcp/tools/task.py — agents are asked for Background/
Implementation/Reasoning/Affects/Limitations/Concerns). The Decisions
tab shows only the summary collapsed and reveals the rest on an explicit
click.

This is embedded JS inside a Python f-string (the page is built and
returned as one big HTML string, not a template file) — there is no
pytest-friendly way to execute it, so most of these are static-source
checks, same approach as the sibling PHP-template guards (e.g.
test_board_header_audit.py). A previous version of this exact code had
TWO real bugs from single-vs-double backslash confusion inside a
non-raw f-string:

1. An unescaped "\\n" inside a JS regex character class
   (``[^*<>\\n]``, written with only ONE backslash in the Python
   source) got consumed by Python's own string escaping into an actual
   newline CHARACTER, splitting the JS regex literal across two lines —
   a JavaScript SyntaxError (line terminators aren't allowed inside a
   regex literal).
2. The same mistake inside a `//` comment describing the note format
   ("<one-line summary>\\n\\n<labeled sections>", again ONE backslash)
   silently turned into two real newlines, ending the `//` comment two
   lines early and leaving its tail ("<labeled sections>...") as bare
   text that JS tried to parse as code.

Both were only caught by rendering the f-string with dummy values and
running the result through `node --check`. That full check is also
included below, but skipped when `node` isn't on PATH (this repo's CI
only sets up Python — see .github/workflows/tests.yml) so it never
blocks the suite; the static checks alongside it catch the same two
historical mistakes without needing Node at all.
"""

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

SERVER_PY = Path(__file__).resolve().parents[3] / "src/marcus_mcp/server.py"


def _get_decisions_page_fstring_source() -> str:
    """Return the exact source text of the `page = f"..."` f-string
    inside project_description_page (there are two "page = f\"\"\"..."
    assignments in this file; picks the one nearest that function)."""
    src = SERVER_PY.read_text()
    tree = ast.parse(src)
    matches = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Name)
                    and t.id == "page"
                    and isinstance(node.value, ast.JoinedStr)
                ):
                    matches.append(node)
    assert matches, "no `page = f\"...\"` assignment found in server.py"
    # The decisions tab lives in project_description_page, which defines
    # the FIRST such f-string in the file.
    target = min(matches, key=lambda m: m.lineno)
    segment = ast.get_source_segment(src, target.value)
    assert segment is not None
    return segment


def _render_decisions_page_html() -> str:
    """Evaluate the extracted f-string with dummy values for every name
    it references, returning the resulting HTML string."""
    segment = _get_decisions_page_fstring_source()
    code = compile(segment, "<fstring>", "eval")
    env = {
        "pid": 7,
        "badge_html": "<div>badge</div>",
        "escaped": "hello world",
        "history_items": [1, 2],
        "history_html": "<div>hist</div>",
        "api_url": "http://x/api/project-description",
        "len": len,
    }
    return eval(code, {}, env)  # noqa: S307 - trusted source, test-only


def _extract_script_block(html: str) -> str:
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>")
    return html[start:end]


def test_decisions_fstring_is_valid_python():
    """Sanity: the f-string must at least compile as a Python expression
    (this alone would NOT have caught either historical bug below —
    both produced syntactically valid Python that broke the JS it
    renders — but it's a cheap first check)."""
    compile(_get_decisions_page_fstring_source(), "<fstring>", "eval")


def test_regex_character_class_escapes_n_with_two_backslashes():
    """Regression #1 (see module docstring): `[^*<>\\n]` in the RAW
    f-string source needs two backslashes — one single backslash there
    gets consumed by Python's own escaping into an actual newline
    character, splitting the JS regex literal across two lines."""
    script = _extract_script_block(_render_decisions_page_html())
    assert "boldLabels" in script
    idx = script.index("[^*<>")
    following = script[idx : idx + 8]
    assert "\\n" in following
    assert "\n" not in following.replace("\\n", "")


def test_comment_about_note_format_does_not_leak_a_bare_html_looking_line():
    """Regression #2 (see module docstring): describing the note format
    with a literal "\\n\\n" inside a `//` comment (one backslash) gets
    converted by Python's f-string parsing into two REAL newlines,
    ending the comment two lines early and leaving its tail
    ("<labeled sections>...") as a bare, non-commented line that isn't
    valid JavaScript on its own. Checks for exactly that historical
    leaked-fragment signature, and generally: no line in the rendered
    script may start with a bare `<` outside of a string/comment (no
    legitimate JS statement starts that way; a leaked HTML-ish comment
    fragment does)."""
    script = _extract_script_block(_render_decisions_page_html())
    assert "<one-line" not in script
    assert "<labeled" not in script
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("<") and not stripped.startswith("<!--"):
            pytest.fail(f"line looks like a leaked HTML/comment fragment: {stripped!r}")


def test_rendered_script_has_balanced_braces():
    """A stray un-doubled `{` or `}` in the Python f-string either raises
    at compile time (usually) or silently swallows/duplicates JS code
    (if the stray braces happen to bound valid Python syntax) — this
    counts braces in the FINAL rendered JS, which must balance."""
    script = _extract_script_block(_render_decisions_page_html())
    assert script.count("{") == script.count("}")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not on PATH")
def test_rendered_script_is_valid_javascript(tmp_path):
    """Strongest available check, when Node happens to be installed:
    actually parse the rendered <script> block as JavaScript. This is
    what originally caught both historical bugs above; the static
    checks in this file exist so the same class of mistake is still
    caught in environments (this repo's CI) with no Node.js at all."""
    script = _extract_script_block(_render_decisions_page_html())
    js_file = tmp_path / "decisions.js"
    js_file.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
