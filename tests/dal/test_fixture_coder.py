"""DAL-R07A: the deterministic no-model fixture coder.

The fixture coder's whole job is to make one repo-declared tracked file change,
deterministically, so the vertical slice has a real diff to verify without a
provider. These tests pin that it writes exactly what the manifest declares,
substitutes the feature id, and refuses any path that could escape the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent_dal.worker.fixture_coder import apply_fixture_change
from personal_agent_dal.worker.toolchain import FixtureCoderSpec


def test_writes_the_declared_file_with_the_feature_id_substituted(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("original\n")
    spec = FixtureCoderSpec(path="README.md", template="fixture {feature_id}\n")

    changed = apply_fixture_change(tmp_path, spec, "feat-1")

    assert changed == ("README.md",)
    assert (tmp_path / "README.md").read_text() == "fixture feat-1\n"


def test_a_template_without_a_placeholder_is_literal(tmp_path: Path) -> None:
    spec = FixtureCoderSpec(path="README.md", template="constant\n")
    apply_fixture_change(tmp_path, spec, "feat-1")
    assert (tmp_path / "README.md").read_text() == "constant\n"


def test_creates_missing_parent_directories(tmp_path: Path) -> None:
    spec = FixtureCoderSpec(path="docs/note.md", template="x\n")
    apply_fixture_change(tmp_path, spec, "f")
    assert (tmp_path / "docs" / "note.md").read_text() == "x\n"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "  ",
        "../escape.md",
        "/etc/passwd",
        ".git/config",
        "a/../../b.md",
        "README.md/..",
    ],
)
def test_refuses_an_unsafe_or_escaping_path(tmp_path: Path, path: str) -> None:
    spec = FixtureCoderSpec(path=path, template="x\n")
    with pytest.raises(ValueError):
        apply_fixture_change(tmp_path, spec, "f")


def test_refuses_a_path_that_names_a_directory(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    spec = FixtureCoderSpec(path="d", template="x\n")
    with pytest.raises(ValueError):
        apply_fixture_change(tmp_path, spec, "f")
