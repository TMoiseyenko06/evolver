"""Tests for the .env loader."""

import os

from evolver.env import find_dotenv, load_dotenv


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_keys_and_strips_quotes_and_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("EVOLVER_MODEL", raising=False)
    env = _write(
        tmp_path / ".env",
        "\n".join([
            "# a comment",
            "",
            "OPENROUTER_API_KEY=sk-or-secret",
            'EVOLVER_MODEL="anthropic/claude-opus-4.8"',
            "export EVOLVER_DATA_DIR='/tmp/data'",
        ]),
    )
    loaded = load_dotenv(env)
    assert loaded["OPENROUTER_API_KEY"] == "sk-or-secret"
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-secret"
    assert os.environ["EVOLVER_MODEL"] == "anthropic/claude-opus-4.8"  # quotes stripped
    assert os.environ["EVOLVER_DATA_DIR"] == "/tmp/data"  # export + quotes handled


def test_existing_env_takes_precedence_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-env-value")
    env = _write(tmp_path / ".env", "OPENROUTER_API_KEY=from-file")
    loaded = load_dotenv(env)
    assert "OPENROUTER_API_KEY" not in loaded  # not overridden
    assert os.environ["OPENROUTER_API_KEY"] == "real-env-value"


def test_override_true_replaces_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVER_MODEL", "old")
    env = _write(tmp_path / ".env", "EVOLVER_MODEL=new")
    load_dotenv(env, override=True)
    assert os.environ["EVOLVER_MODEL"] == "new"


def test_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "does-not-exist.env") == {}


def test_find_dotenv_walks_up(tmp_path):
    _write(tmp_path / ".env", "X=1")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_dotenv(nested) == tmp_path / ".env"


def test_config_reads_loaded_env(tmp_path, monkeypatch):
    monkeypatch.delenv("EVOLVER_MODEL", raising=False)
    env = _write(tmp_path / ".env", "EVOLVER_MODEL=test/from-dotenv")
    load_dotenv(env, override=True)
    from evolver.config import Config

    assert Config().model == "test/from-dotenv"
