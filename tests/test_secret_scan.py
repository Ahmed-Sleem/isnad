"""Secret scanning must detect synthetic credentials and allow empty placeholders."""

from pathlib import Path

from scripts.secret_scan import scan_paths, scan_text


def test_detects_synthetic_github_token_shape_without_echoing_it() -> None:
    text = "credential=ghp_" + ("A" * 30)

    issues = scan_text(text, path="fixture.txt")

    assert len(issues) == 1
    assert issues[0].path == "fixture.txt"
    assert issues[0].rule == "github-token"
    assert "A" not in repr(issues[0])


def test_detects_synthetic_credential_assignment_without_echoing_it() -> None:
    assignment = "API" + "_KEY="
    secret = "B" * 24

    issues = scan_text(assignment + secret, path="fixture.txt")

    assert len(issues) == 1
    assert issues[0].rule == "credential-assignment"
    assert secret not in repr(issues[0])


def test_literal_environment_variable_references_are_not_secrets() -> None:
    line = "api" + "_key: os.environ/" + "OPENAI_API_KEY"

    assert scan_text(line, path="config.yaml") == []
    secret_assignment = "pass" + "word: "
    assert scan_text(line + "\n" + secret_assignment + ("C" * 24), path="config.yaml")


def test_empty_environment_placeholders_are_not_secrets(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("\n".join(("API_KEY=", "ACCESS_TOKEN=")) + "\n", encoding="utf-8")

    assert scan_paths(tmp_path, [Path(".env.example")]) == []


def test_private_key_file_path_is_rejected(tmp_path: Path) -> None:
    key_file = tmp_path / "service.key"
    key_file.write_text("placeholder", encoding="utf-8")

    issues = scan_paths(tmp_path, [Path("service.key")])

    assert len(issues) == 1
    assert issues[0].rule == "sensitive-file-path"
