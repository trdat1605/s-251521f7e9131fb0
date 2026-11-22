"""Safety boundaries and archive contract; Docker behavior is tested by metadata-regression."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).with_name("bootstrap.py"))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_")}
        self.git("init")
        (self.root / ".gitignore").write_text(".codex/\n.env\n")
        (self.root / "app.rb").write_text("puts 'fixture'\n")
        self.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.root), *arguments],
                                       env=self.environment, stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-m", "fixture")

    def test_project_and_port_boundary(self):
        bootstrap.validate_project("cwdev-complete001")
        for project in ("cwlab-9200023e", "course-platform-local", "../cwdev-test",
                        "cwdev-course", "cwdev-cwlab", "cwdev-UPPER", "cwdev-"):
            with self.subTest(project=project), self.assertRaises(ValueError):
                bootstrap.validate_project(project)
        self.assertEqual(bootstrap.validate_ports(44440), [44440, 44441, 44442])
        for port in (3000, 44330, 44439, 65534):
            with self.subTest(port=port), self.assertRaises(ValueError):
                bootstrap.validate_ports(port)

    def test_rejects_unstaged_staged_and_untracked_source(self):
        original = bootstrap.source_head(self.root)
        self.assertRegex(original, r"^[0-9a-f]{40}$")
        (self.root / "app.rb").write_text("changed")
        with self.assertRaises(ValueError):
            bootstrap.source_head(self.root)

        self.git("add", "app.rb")
        with self.assertRaises(ValueError):
            bootstrap.source_head(self.root)
        self.commit()
        (self.root / "untracked").write_text("new")
        with self.assertRaises(ValueError):
            bootstrap.source_head(self.root)

    def test_git_environment_cannot_redirect_source_identity(self):
        expected = self.git("rev-parse", "HEAD")
        with patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent"}):
            self.assertEqual(bootstrap.source_head(self.root), expected)

    def test_archive_contains_tracked_source_not_git_or_ignored_files(self):
        (self.root / ".env").write_text("FAKE_SECRET=should-not-copy")
        (self.root / ".codex").mkdir()
        (self.root / ".codex" / "adapter").write_text("local")
        target = self.root / ".codex" / "snapshot"
        target.mkdir()
        bootstrap.archive_source(self.root, target)
        self.assertTrue((target / "app.rb").is_file())
        self.assertFalse((target / ".git").exists())
        self.assertFalse((target / ".env").exists())
        self.assertFalse((target / ".codex").exists())

    def test_tracked_environment_rejected_before_extraction(self):
        (self.root / ".env").write_text("FAKE_SECRET=never-copy")
        self.git("add", "-f", ".env")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-m", "bad tracked fixture")
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaises(ValueError):
                bootstrap.archive_source(self.root, Path(target))
            self.assertEqual(list(Path(target).iterdir()), [])

    def test_prepare_private_secrets_idempotent_and_stale_ref_rejected(self):
        empty = subprocess.CompletedProcess([], 0, stdout="")
        real_run = bootstrap.run

        def no_docker(arguments, **options):
            if arguments[0] == "docker":
                return empty
            return real_run(arguments, **options)

        with patch.object(bootstrap, "check_ports"), patch.object(bootstrap, "run", side_effect=no_docker):
            with contextlib.redirect_stdout(io.StringIO()):
                bootstrap.prepare(self.root, "cwdev-test", 44440, None)
                bootstrap.prepare(self.root, "cwdev-test", 44440, None)
        directory = self.root / ".codex" / "isolated" / "cwdev-test"
        secret_file = directory / "secrets.env"
        self.assertEqual(secret_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(secret_file.read_text().splitlines()), 3)
        self.assertNotIn("SECRET_KEY_BASE", (directory / "state.json").read_text())
        self.assertEqual((directory / "source/.git_sha").read_text().strip(), self.git("rev-parse", "HEAD"))
        state = bootstrap.read_state(self.root, "cwdev-test")
        self.assertEqual(state["dependency_mode"], "FULL_BUILD")
        state["project"] = "cwlab-9200023e"
        (directory / "state.json").write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            bootstrap.read_state(self.root, "cwdev-test")
        state["project"] = "cwdev-test"
        (directory / "state.json").write_text(json.dumps(state))
        (self.root / "app.rb").write_text("new committed ref")
        self.commit()
        with self.assertRaises(ValueError):
            bootstrap.read_state(self.root, "cwdev-test")

    def test_reuse_requires_exact_image_identity(self):
        for image in ("cwlab:latest", "sha256:abc", "-p cwlab", ""):
            if not image:
                continue
            with self.subTest(image=image), self.assertRaises(ValueError):
                bootstrap.prepare(self.root, "cwdev-test", 44440, image)

    def test_busy_port_rejected(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            with self.assertRaises(OSError):
                bootstrap.check_ports([sock.getsockname()[1]])

    def test_symlinked_state_outside_checkout_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            (self.root / ".codex").symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                bootstrap.state_dir(self.root, "cwdev-test")

    def test_compose_structure_overrides_ambient_settings(self):
        state = {"project": "cwdev-test", "source_commit": "a" * 40,
                 "reuse_base_image": None, "ports": [44440, 44441, 44442]}
        with patch.dict(os.environ, {"COMPOSE_FILE": "/untrusted.yml",
                                    "COMPOSE_PROJECT_NAME": "cwlab-9200023e",
                                    "CW_SOURCE": "/untrusted", "POSTGRES_PASSWORD": "fake"}):
            with patch.object(bootstrap, "run") as invoke:
                bootstrap.compose(self.root, state, ["config", "--quiet"])
        command = invoke.call_args.args[0]
        environment = invoke.call_args.kwargs["env"]
        self.assertEqual(command[command.index("--project-name") + 1], "cwdev-test")
        self.assertEqual(command[command.index("--file") + 1], str(self.root / "docker/isolated/compose.yaml"))
        self.assertNotIn("COMPOSE_FILE", environment)
        self.assertNotIn("POSTGRES_PASSWORD", environment)
        self.assertEqual(environment["CW_SOURCE"], str(self.root / ".codex/isolated/cwdev-test/source"))


if __name__ == "__main__":
    unittest.main()

