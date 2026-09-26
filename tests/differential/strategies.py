"""Hypothesis strategies for adversarial Actions (shared by the differential tests)."""

from __future__ import annotations

from hypothesis import strategies as st

from proofread.contracts import ACTION_KINDS, Action

PATH_PARTS = ["workspace", "tmp", "etc", "home", "tests", "test", "testing", ".github", "..", ".", "",
              "test_x.py", "test_.py", "test_", "x_test.py", "_test.py", "conftest.py", "__init__.py",
              "setup.cfg", "pyproject.toml", "pytest.ini", ".pytest.ini", "tox.ini", "noxfile.py", "Makefile",
              "makefile", "GNUmakefile", "sitecustomize.py", "usercustomize.py", "a.pth", ".pth", "m.pyc", ".pyo", "x.pyc.txt", "func.py",
              "tésts", "Tests", "tést_x.py", "\x00", "workspace2", "src", "x"]

LINE_TOKENS = ["pytest", "pytest.", ".", "mark", "skip", "skipif", "xfail", "exit", " ", "  ", "\t", "(", ")",
               "from", "import", "as", "unittest", "_pytest", "_pytest.outcomes", "outcomes", "skipping",
               "SkipTest", "raise", "Skipped", "__unittest_skip__", "ｓｋｉｐ", "é", "　",
               "\n", "x", "_", "0", ",", "-", "@", "importorskip", "expectedFailure", "skipTest", "skipIf",
               "skipUnless", "skip(", ".skip", " ", " ", "①", "µ", "K", "#", "=",
               "\U0001d42c", "ﬁ", "١"]

SECTIONS = ["", "tool:pytest", "tool.pytest", "tool.pytest.ini_options", "<top>", "project", " , ", ",",
            "　tool:pytest ", "tool:pytest,metadata", "metadata", "tool.pytestx", "TOOL:PYTEST", "tool.black"]

HOSTS = ["", "a.com", "A.COM.", " a.com ", "b.org", "É.fr", "é.fr", "a.com..", ".", "localhost",
         "İ.tr", "a.com　"]


def _path() -> st.SearchStrategy[str]:
    parts = st.lists(st.one_of(st.sampled_from(PATH_PARTS), st.text(max_size=4)), max_size=6)
    lead = st.sampled_from(["/", "//", "///", "", "./", "/workspace/", "/tmp/"])
    return st.one_of(st.just(""), st.builds(lambda l, ps: l + "/".join(ps), lead, parts))


def _line() -> st.SearchStrategy[str]:
    toks = st.lists(st.one_of(st.sampled_from(LINE_TOKENS), st.text(max_size=3)), max_size=10)
    return st.builds("".join, toks)


@st.composite
def actions(draw) -> Action:
    return Action(
        episode_id="diff",
        step=draw(st.integers(min_value=-1, max_value=5)),
        kind=draw(st.sampled_from(ACTION_KINDS)),
        path=draw(_path()),
        dst=draw(st.one_of(st.just(""), _path())),
        added_lines=draw(st.lists(_line(), max_size=4)),
        removed_lines=draw(st.lists(_line(), max_size=2)),
        section=draw(st.one_of(st.sampled_from(SECTIONS), st.text(max_size=6))),
        host=draw(st.one_of(st.sampled_from(HOSTS), st.text(max_size=5))),
        cmd=draw(st.text(max_size=5)),
        attributed=draw(st.booleans()),
        protected_extra=draw(st.lists(_path(), max_size=3)),
    )


allowlists = st.lists(st.sampled_from(HOSTS), max_size=3).map(tuple)
