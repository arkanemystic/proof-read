"""Pure tests of proofread.actions.canonicalize."""

from __future__ import annotations

from hypothesis import given, strategies as st

from proofread.actions.canonicalize import (
    canon_path,
    canon_text,
    changed_sections,
    decode_bytes,
    diff_lines,
    is_under,
    split_lines,
)


def test_canon_path():
    assert canon_path("func.py") == "/workspace/func.py"
    assert canon_path("./a/../b.py") == "/workspace/b.py"
    assert canon_path("//workspace///x") == "/workspace/x"
    assert canon_path("/workspace/../etc/passwd") == "/etc/passwd"
    assert canon_path("") == ""
    assert canon_path("café.py") == "/workspace/café.py"


def test_is_under():
    assert is_under("/workspace/a", "/workspace")
    assert is_under("/workspace", "/workspace")
    assert not is_under("/workspacex/a", "/workspace")


def test_text_normalization():
    assert canon_text("a\r\nb\rc") == "a\nb\nc"
    assert split_lines("x\r\n@pytest.mark.skip\r\n") == ["x", "@pytest.mark.skip"]


def test_diff_lines_multiset():
    added, removed = diff_lines("a\nb\n", "a\nb\nb\nc\n")
    assert added == ["b", "c"] and removed == []
    added, removed = diff_lines("a\nb\n", "b\na\n")
    assert added == [] and removed == []
    added, removed = diff_lines("a\r\nb\r\n", "a\nb\n")
    assert added == [] and removed == []
    assert diff_lines(None, "x") == (["x"], [])


def test_decode_coding_cookie():
    src = "# -*- coding: utf-7 -*-\nx = 1\n".encode("utf-7")
    assert "x = 1" in decode_bytes(src, "a.py")
    assert decode_bytes(b"\xff\xfe", "a.txt")  # replacement, no crash


def test_changed_sections():
    old = "[project]\nname='a'\n\n[tool.pytest.ini_options]\naddopts=''\n"
    new = "[project]\nname='a'\n\n[tool.pytest.ini_options]\naddopts='-k nothing'\n"
    assert changed_sections(old, new, "/workspace/pyproject.toml") == "tool.pytest.ini_options"
    assert changed_sections("", 'tool.pytest.ini_options.addopts = "-x"\n', "pyproject.toml") == "<top>"
    assert changed_sections("[metadata]\nname=a\n", "[metadata]\nname=b\n", "setup.cfg") == "metadata"
    assert changed_sections("", "[tool:pytest]\naddopts=-x\n", "setup.cfg") == "tool:pytest"
    assert changed_sections("a", "b", "func.py") == ""


@given(st.text(max_size=200), st.text(max_size=200))
def test_diff_lines_roundtrip(old, new):
    added, removed = diff_lines(old, new)
    from collections import Counter

    assert Counter(split_lines(old)) - Counter(removed) + Counter(added) == Counter(split_lines(new))


@given(st.text(alphabet="ab/.", max_size=30))
def test_canon_path_idempotent(p):
    c = canon_path(p)
    assert canon_path(c) == c
    assert ".." not in c.split("/") or c == ""
