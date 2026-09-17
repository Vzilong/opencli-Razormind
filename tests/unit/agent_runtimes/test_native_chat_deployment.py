import importlib.util
import sqlite3
from pathlib import Path

import pytest


def load_deployment_module(name):
    source = Path(__file__).resolve().parents[3] / "agent" / "native-chat" / f"{name}.py"
    specification = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_auth_export_copies_only_auth_rows(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "isolated" / "agent.db"
    with sqlite3.connect(source) as database:
        database.execute(
            "CREATE TABLE auth_credentials(id INTEGER, provider TEXT, "
            "credential_type TEXT, disabled_cause TEXT, data TEXT)"
        )
        database.executemany(
            "INSERT INTO auth_credentials VALUES (?,?,?,?,?)",
            [
                (1, "authorized", "oauth", None, "selected"),
                (2, "other-provider", "oauth", None, "must-not-copy"),
                (3, "authorized", "oauth", "disabled", "must-not-copy"),
                (4, "authorized", "oauth", None, "sibling-must-not-copy"),
            ],
        )
        for table in (
            "auth_schema_version",
            "auth_change_revision",
            "settings",
            "history",
        ):
            database.execute(f"CREATE TABLE {table}(value TEXT)")
            database.execute(f"INSERT INTO {table} VALUES (?)", (table,))
    exporter = load_deployment_module("provision_auth")
    with pytest.raises(ValueError, match="explicitly authorized"):
        exporter.provision(source, destination, "omp")
    with pytest.raises(ValueError, match="exactly one"):
        exporter.provision(source, destination, "omp", provider="authorized")
    with pytest.raises(ValueError, match="exactly one"):
        exporter.provision(source, destination, "omp", provider="authorized", account_id=3)
    assert not destination.exists()
    exporter.provision(source, destination, "omp", provider="authorized", account_id=1)
    with sqlite3.connect(destination) as isolated:
        assert isolated.execute("SELECT id, data FROM auth_credentials").fetchall() == [
            (1, "selected")
        ]
        assert isolated.execute("SELECT COUNT(*) FROM settings").fetchone() == (0,)
        assert isolated.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
    with sqlite3.connect(source) as original:
        assert original.execute("SELECT COUNT(*) FROM settings").fetchone() == (1,)


def test_auth_export_failure_leaves_no_destination(tmp_path):
    source = tmp_path / "invalid.db"
    source.write_text("not sqlite")
    destination = tmp_path / "isolated" / "agent.db"
    with pytest.raises(sqlite3.DatabaseError):
        load_deployment_module("provision_auth").provision(
            source, destination, "omp", provider="authorized"
        )
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_auth_export_never_overwrites_existing_credentials(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("new credentials")
    destination = tmp_path / "existing.json"
    destination.write_text("existing credentials")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        load_deployment_module("provision_auth").provision(source, destination, "codex")
    assert destination.read_text() == "existing credentials"


@pytest.mark.parametrize("runtime", ["codex", "omp"])
def test_runner_isolates_environment_credentials_and_worktree(tmp_path, monkeypatch, runtime):
    runner = load_deployment_module("runner")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AGENT_API_TOKEN", "must-not-leak")
    arguments = runner.command(runtime, ["--operator-chat"])
    assert arguments[0] == "/usr/bin/bwrap"
    assert "--clearenv" in arguments and "--unshare-all" in arguments
    assert arguments[arguments.index("--cap-drop") + 1] == "ALL"
    assert "AGENT_API_TOKEN" not in arguments and "must-not-leak" not in arguments
    credential_mount = arguments.index(str(tmp_path / "credentials" / runtime))
    assert arguments[credential_mount - 1] == "--bind"
    other_runtime = "omp" if runtime == "codex" else "codex"
    assert str(tmp_path / "credentials" / other_runtime) not in arguments
    work_mount = arguments.index(str(work))
    assert arguments[work_mount - 1] == "--ro-bind"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        runner.command(runtime, ["--operator-chat"])


@pytest.mark.parametrize(
    ("runtime", "binary"),
    [("codex", "/runtime/node_modules/.bin/codex"), ("omp", "/runtime/node_modules/.bin/omp")],
)
def test_runner_terminal_mode_executes_real_cli(tmp_path, monkeypatch, runtime, binary):
    runner = load_deployment_module("runner")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    arguments = runner.command(runtime, ["--terminal", "--example", "prompt"])

    separator = arguments.index("--")
    assert arguments[arguments.index("TERM") + 1] == "xterm-256color"
    assert arguments[separator + 1] == binary
    assert arguments[separator + 2 :] == ["--example", "prompt"]
    assert "/runtime/omp-inference.ts" not in arguments
