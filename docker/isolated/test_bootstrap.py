"""Safety boundaries and archive contract; Docker behavior is tested by metadata-regression."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
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

    def test_real_upstream_rule_shape_omitted_and_normal_internal_link_preserved(self):
        (self.root / "AGENTS.md").write_text("fixture instructions retained")
        rules = self.root / ".windsurf/rules"
        rules.mkdir(parents=True)
        # Exact path/type/target from upstream caab07a8; this is an internal
        # symlink. Local older tarfile incorrectly treats its target as root-relative.
        (rules / "chatwoot.md").symlink_to("../../AGENTS.md")
        (self.root / "CLAUDE.md").symlink_to("AGENTS.md")
        self.commit()
        with tempfile.TemporaryDirectory() as target:
            destination = Path(target)
            bootstrap.archive_source(self.root, destination)
            self.assertFalse((destination / ".windsurf/rules/chatwoot.md").is_symlink())
            self.assertTrue((destination / "AGENTS.md").is_file())
            self.assertTrue((destination / "CLAUDE.md").is_symlink())
            self.assertEqual((destination / "CLAUDE.md").readlink(), Path("AGENTS.md"))

    def test_changed_known_rule_shape_fails_closed(self):
        rules = self.root / ".windsurf/rules"
        rules.mkdir(parents=True)
        # Neither a changed link target nor a regular file is silently omitted.
        for variant in ("../../../AGENTS.md", None):
            path = rules / "chatwoot.md"
            if variant is None:
                path.write_text("changed upstream type")
            else:
                path.symlink_to(variant)
            self.commit()
            with tempfile.TemporaryDirectory() as target, self.assertRaises(ValueError):
                bootstrap.archive_source(self.root, Path(target))
            path.unlink()

    def test_unrelated_external_symlink_still_rejected(self):
        (self.root / "external-rule").symlink_to("../outside-archive")
        self.commit()
        with tempfile.TemporaryDirectory() as target, self.assertRaises(tarfile.LinkOutsideDestinationError):
            bootstrap.archive_source(self.root, Path(target))

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

    def test_temporary_named_image_reference_verified_and_only_owned_tag_removed(self):
        selected = "sha256:" + "a" * 64
        with patch.object(bootstrap, "image_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", return_value=["fixture:original"]), \
                patch.object(bootstrap, "tag_id", side_effect=[None, selected, selected, selected]), \
                patch.object(bootstrap, "run") as invoke, contextlib.redirect_stdout(io.StringIO()):
            with bootstrap.borrowed_image_tag(selected) as reference:
                self.assertRegex(reference, r"^cwdev-base-[0-9a-f]{32}:selected$")
                self.assertFalse(reference.startswith("sha256:"))
        self.assertEqual(invoke.call_args_list[0].args[0], ["docker", "image", "tag", selected, reference])
        self.assertEqual(invoke.call_args_list[1].args[0], ["docker", "image", "rm", "--no-prune", reference])

    def test_retargeted_temporary_tag_is_not_removed(self):
        selected = "sha256:" + "a" * 64
        changed = "sha256:" + "b" * 64
        with patch.object(bootstrap, "image_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", return_value=["fixture:original"]), \
                patch.object(bootstrap, "tag_id", side_effect=[None, selected, changed, changed]), \
                patch.object(bootstrap, "run") as invoke, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                with bootstrap.borrowed_image_tag(selected):
                    pass
        self.assertEqual(len(invoke.call_args_list), 1)  # Only creation, never removal.

    def test_existing_temporary_reference_never_overwritten(self):
        selected = "sha256:" + "a" * 64
        with patch.object(bootstrap, "image_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", return_value=["fixture:original"]), \
                patch.object(bootstrap, "tag_id", return_value=selected), \
                patch.object(bootstrap, "run") as invoke:
            with self.assertRaises(ValueError):
                with bootstrap.borrowed_image_tag(selected):
                    self.fail("existing reference must not enter build context")
        invoke.assert_not_called()

    def test_build_failure_still_removes_only_its_verified_temporary_tag(self):
        selected = "sha256:" + "a" * 64
        with patch.object(bootstrap, "image_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", return_value=["fixture:original"]), \
                patch.object(bootstrap, "tag_id", side_effect=[None, selected, selected]), \
                patch.object(bootstrap, "run") as invoke, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                with bootstrap.borrowed_image_tag(selected) as reference:
                    raise RuntimeError("synthetic build failure")
        self.assertEqual(invoke.call_args_list[-1].args[0], ["docker", "image", "rm", "--no-prune", reference])
        with patch.object(bootstrap, "run") as invoke:
            with self.assertRaises(ValueError):
                bootstrap.remove_owned_tag("cwlab-9200023e:base", selected)
            invoke.assert_not_called()

    def test_untagged_base_image_rejected_without_docker_mutation(self):
        selected = "sha256:" + "a" * 64
        with patch.object(bootstrap, "image_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", return_value=[]), \
                patch.object(bootstrap, "run") as invoke:
            with self.assertRaisesRegex(ValueError, "independent named tag"):
                with bootstrap.borrowed_image_tag(selected):
                    self.fail("untagged image must never be borrowed")
        invoke.assert_not_called()

    def test_last_tag_cleanup_refused_when_original_tag_disappears(self):
        selected = "sha256:" + "a" * 64
        reference = "cwdev-base-" + "c" * 32 + ":selected"
        fake_uuid = unittest.mock.Mock(hex="c" * 32)
        with patch.object(bootstrap, "image_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", side_effect=[["fixture:original"], [reference]]), \
                patch.object(bootstrap.uuid, "uuid4", return_value=fake_uuid), \
                patch.object(bootstrap, "tag_id", side_effect=[None, selected, selected, selected]), \
                patch.object(bootstrap, "run") as invoke, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "temporary tag retained"):
                with bootstrap.borrowed_image_tag(selected):
                    pass
        self.assertEqual(invoke.call_args_list, [unittest.mock.call(
            ["docker", "image", "tag", selected, reference], capture=True)])

    def test_cleanup_refused_when_independent_tag_resolves_to_another_image(self):
        selected = "sha256:" + "a" * 64
        changed = "sha256:" + "b" * 64
        reference = "cwdev-base-" + "c" * 32 + ":selected"
        with patch.object(bootstrap, "tag_id", return_value=selected), \
                patch.object(bootstrap, "image_repo_tags", return_value=[reference, "fixture:original"]), \
                patch.object(bootstrap, "image_id", return_value=changed), \
                patch.object(bootstrap, "run") as invoke:
            with self.assertRaisesRegex(ValueError, "temporary tag retained"):
                bootstrap.remove_owned_tag(reference, selected, preserve_image=True)
        invoke.assert_not_called()

    def test_cached_build_passes_named_base_reference_and_retains_exact_id_selection(self):
        selected = "sha256:" + "a" * 64
        reference = "cwdev-base-" + "c" * 32 + ":selected"
        state = {"project": "cwdev-test", "source_commit": "b" * 40, "reuse_base_image": selected}
        with patch.object(bootstrap, "borrowed_image_tag", return_value=contextlib.nullcontext(reference)) as select, \
                patch.object(bootstrap, "run") as invoke:
            bootstrap.build_app(self.root, state)
        select.assert_called_once_with(selected)
        command = invoke.call_args.args[0]
        self.assertIn(f"CHATWOOT_BASE_IMAGE={reference}", command)
        self.assertIn("CHATWOOT_SOURCE_COMMIT=" + "b" * 40, command)
        self.assertNotIn(f"CHATWOOT_BASE_IMAGE={selected}", command)

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
