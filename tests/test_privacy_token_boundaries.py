"""Regression coverage for local paths embedded in upload report prose."""

import json

import pytest

from qajev_upload_privacy import (
    MASK,
    public_reference,
    remember_paths,
    restore_paths,
    sanitize_upload,
)


def test_unquoted_paths_preserve_report_labels_and_following_reasons():
    source = "/Users/alice/work/qa.yaml"
    run_dir = "/home/bob/.qajev/runs/1"
    other = r"C:\Users\carol\x"
    prose = f"source: {source} run_dir={run_dir} {other} because the check failed"
    paths = {}

    clean = sanitize_upload(prose.encode(), paths=paths).decode()

    assert clean == (
        f"source: {public_reference(source)} run_dir={public_reference(run_dir)} "
        f"{public_reference(other)} because the check failed"
    )
    assert set(paths.values()) == {source, run_dir, other}


@pytest.mark.parametrize("separator", [" ", "\t", "\n"])
def test_unquoted_absolute_paths_stop_at_whitespace(separator):
    path = "/home/dana/results/check.json"

    clean = sanitize_upload(f"artifact: {path}{separator}retry required".encode()).decode()

    assert clean == f"artifact: {public_reference(path)}{separator}retry required"


def test_unquoted_file_urls_preserve_following_paths_and_explanation():
    run_dir = "/home/bob/.qajev/runs/1"
    other = r"C:\Users\carol\x"
    prose = (
        f"source: file:///Users/alice/work/qa.yaml run_dir={run_dir} "
        f"{other} because the check failed"
    )
    paths = {}

    clean = sanitize_upload(prose.encode(), paths=paths).decode()

    assert clean == (
        f"source: {MASK} run_dir={public_reference(run_dir)} "
        f"{public_reference(other)} because the check failed"
    )
    assert set(paths.values()) == {run_dir, other}


@pytest.mark.parametrize("quote", ['"', "'", "`"])
@pytest.mark.parametrize(
    "path", ["/Users/alice/Work Files/qa.yaml", r"C:\Users\carol\Work Files\qa.yaml"]
)
def test_quoted_paths_keep_spaces_but_stop_at_the_closing_quote(quote, path):
    other = "/home/bob/.qajev/runs/1"
    prose = f"source: {quote}{path}{quote} because it failed run_dir={other}"
    paths = {}

    clean = sanitize_upload(prose.encode(), paths=paths).decode()

    assert clean == (
        f"source: {quote}{public_reference(path)}{quote} because it failed "
        f"run_dir={public_reference(other)}"
    )
    assert set(paths.values()) == {path, other}


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_quoted_file_urls_keep_the_explanation_outside_the_quote(quote):
    other = r"C:\Users\carol\x"
    prose = (
        f"source: {quote}file:///Users/alice/Work Files/qa.yaml{quote} "
        f"because it failed run_dir={other}"
    )

    clean = sanitize_upload(prose.encode()).decode()

    assert clean == (
        f"source: {quote}{MASK}{quote} because it failed run_dir={public_reference(other)}"
    )


@pytest.mark.parametrize(
    "path", ["/Users/alice/Work Files/qa.yaml", r"C:\Users\carol\Work Files\qa.yaml"]
)
def test_public_reference_keeps_a_spaced_path_as_one_opaque_reference(path):
    reference = public_reference(path)

    assert reference.startswith("local/")
    assert len(reference) == len("local/") + 24
    assert public_reference(reference) == reference


def test_json_source_and_run_dir_with_spaces_restore_from_the_private_registry(tmp_path):
    original = {
        "source": "/Users/alice/Work Files/qa.yaml",
        "run_dir": r"C:\Users\carol\Run Files\trial one",
        "reason": "the check failed; retry required",
    }
    paths = {}

    clean = json.loads(sanitize_upload(json.dumps(original).encode(), paths=paths))

    assert clean == {
        "source": public_reference(original["source"]),
        "run_dir": public_reference(original["run_dir"]),
        "reason": original["reason"],
    }
    assert set(paths.values()) == {original["source"], original["run_dir"]}
    remember_paths(paths, directory=tmp_path)
    assert restore_paths(clean, directory=tmp_path) == original


@pytest.mark.parametrize(
    "field", ["source", "run_dir"]
)
def test_json_file_url_fields_hide_the_complete_spaced_location(field):
    original = {field: "file:///Users/alice/Work Files/qa.yaml", "reason": "retry required"}

    clean = json.loads(sanitize_upload(json.dumps(original).encode()))

    assert clean == {field: MASK, "reason": "retry required"}


@pytest.mark.parametrize(
    "repository", ["https://github.com/example/project.git", "github.com:example/project.git"]
)
def test_network_repository_identifiers_keep_the_existing_contract(repository):
    original = {"repository": repository}

    assert json.loads(sanitize_upload(json.dumps(original).encode())) == original
