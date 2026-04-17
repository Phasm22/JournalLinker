import contextlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ENV_MODULE_PATH = Path(__file__).resolve().parents[1] / "journal_linker_env.py"
spec = importlib.util.spec_from_file_location("journal_linker_env", ENV_MODULE_PATH)
journal_linker_env = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(journal_linker_env)


class TestJournalLinkerEnvBootstrap(unittest.TestCase):
    @staticmethod
    def _minimal_os_environ() -> dict[str, str]:
        """Keep a tiny, deterministic environment for bootstrap tests."""
        keep = [
            "HOME",
            "PATH",
            "LANG",
            "USER",
            "LOGNAME",
            "SHELL",
            "XDG_RUNTIME_DIR",
        ]
        out: dict[str, str] = {}
        for k in keep:
            v = os.environ.get(k)
            if v:
                out[k] = v
        return out

    @staticmethod
    @contextlib.contextmanager
    def _isolated_environ(overrides: dict[str, str] | None = None, *, xdg_config_home: str | None = None):
        """Hard-isolate os.environ for deterministic tests (patch.dict can be leaky)."""
        before = os.environ.copy()
        os.environ.clear()
        try:
            os.environ.update(TestJournalLinkerEnvBootstrap._minimal_os_environ())
            # Prevent accidental reads of the developer's real ~/.config during tests.
            if xdg_config_home is not None:
                os.environ["XDG_CONFIG_HOME"] = xdg_config_home
            if overrides:
                os.environ.update(overrides)
            yield
        finally:
            os.environ.clear()
            os.environ.update(before)

    def test_prefers_journal_linker_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()

            env_file = Path(d) / "secrets.env"
            env_file.write_text("SCRIBE_JOURNAL_DIR=/from_file\n", encoding="utf-8")

            fake_xdg = str(Path(d) / "xdg")
            Path(fake_xdg).mkdir(parents=True, exist_ok=True)
            with self._isolated_environ({"JOURNAL_LINKER_ENV_FILE": str(env_file)}, xdg_config_home=fake_xdg):
                journal_linker_env.bootstrap_journal_linker_env(repo_root=repo)
                self.assertEqual(os.environ.get("SCRIBE_JOURNAL_DIR"), "/from_file")

    def test_does_not_override_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()

            env_file = Path(d) / "secrets.env"
            env_file.write_text("SCRIBE_JOURNAL_DIR=/from_file\n", encoding="utf-8")

            fake_xdg = str(Path(d) / "xdg")
            Path(fake_xdg).mkdir(parents=True, exist_ok=True)
            with self._isolated_environ(
                {"SCRIBE_JOURNAL_DIR": "/from_env", "JOURNAL_LINKER_ENV_FILE": str(env_file)},
                xdg_config_home=fake_xdg,
            ):
                journal_linker_env.bootstrap_journal_linker_env(repo_root=repo)
                self.assertEqual(os.environ.get("SCRIBE_JOURNAL_DIR"), "/from_env")

    def test_legacy_dotenv_requires_flag(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            (repo / ".env").write_text("SCRIBE_JOURNAL_DIR=/legacy\n", encoding="utf-8")

            fake_xdg = str(Path(d) / "xdg")
            Path(fake_xdg).mkdir(parents=True, exist_ok=True)
            with self._isolated_environ(xdg_config_home=fake_xdg):
                journal_linker_env.bootstrap_journal_linker_env(repo_root=repo)
                self.assertNotIn("SCRIBE_JOURNAL_DIR", os.environ)

                os.environ.pop("JOURNAL_LINKER_ENV_BOOTSTRAPPED", None)
                os.environ["JOURNAL_LINKER_DOTENV"] = "1"
                journal_linker_env.bootstrap_journal_linker_env(repo_root=repo)
                self.assertEqual(os.environ.get("SCRIBE_JOURNAL_DIR"), "/legacy")

    def test_falls_back_to_legacy_xdg_filename(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config"
            jl_dir = cfg / "journal-linker"
            jl_dir.mkdir(parents=True)

            legacy_file = jl_dir / "env"
            legacy_file.write_text("SCRIBE_JOURNAL_DIR=/xdg_legacy\n", encoding="utf-8")

            with self._isolated_environ(xdg_config_home=str(cfg)):
                repo = Path(d) / "repo"
                repo.mkdir()
                journal_linker_env.bootstrap_journal_linker_env(repo_root=repo)
                self.assertEqual(os.environ.get("SCRIBE_JOURNAL_DIR"), "/xdg_legacy")
