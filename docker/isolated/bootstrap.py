#!/usr/bin/env python3
"""Prepare and operate only a new, project-scoped Chatwoot development snapshot."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid

BASELINE = "caab07a8f00032d5205b5a5d7e2eb0ba2f3d2d82"
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def run(args, cwd=None, capture=False, env=None, check=True):
    return subprocess.run(args, cwd=cwd, env=env, check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None)


def git(root, *args):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return run(["git", "-C", str(root), *args], capture=True, env=environment).stdout.strip()


def validate_project(project):
    if not re.fullmatch(r"cwdev-[a-z0-9][a-z0-9-]{0,39}", project):
        raise ValueError("project must be cwdev- followed by 1..40 lowercase letters/digits/hyphens")
    if "cwlab" in project or "course" in project:
        raise ValueError("protected runtime identity")


def validate_ports(port_base):
    if not 44440 <= port_base <= 65533:
        raise ValueError("port base must be 44440..65533")
    return [port_base, port_base + 1, port_base + 2]


def source_head(root):
    head = git(root, "rev-parse", "HEAD")
    if not SHA.fullmatch(head):
        raise ValueError("Git must return a complete SHA1 commit")
    # Include staged and unstaged tracked modifications; reject untracked files
    # unless Git ignores them (generated state lives under ignored .codex/).
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("commit source changes before preparing or operating a snapshot")
    return head


def archive_source(root, target):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    payload = subprocess.check_output(["git", "-C", str(root), "archive", "--format=tar", "HEAD"], env=environment)
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.parts[0] in {".git", ".codex"} or path.name == "AGENTS.override.md":
                raise ValueError("local adapter/metadata must not be tracked in build context")
            if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
                raise ValueError("non-example environment file must not enter build context")
        archive.extractall(target, filter="data")


def metadata_block(dockerfile):
    start = dockerfile.index("# Generate .git_sha file with current commit hash")
    end = dockerfile.index("# Remove unnecessary files", start)
    return dockerfile[start:end].strip()


def check_ports(ports, owned=()):
    held = []
    try:
        for port in ports:
            if port in owned:
                continue
            sock = socket.socket()
            held.append(sock)
            sock.bind(("127.0.0.1", port))
    finally:
        for sock in held:
            sock.close()


def state_dir(root, project):
    validate_project(project)
    # Refuse a symlinked .codex/state directory that could escape this checkout.
    base = root / ".codex" / "isolated" / project
    if not base.resolve().is_relative_to(root.resolve()):
        raise ValueError("state directory must remain inside this checkout")
    return base


def prepare(root, project, port_base, reuse):
    validate_project(project)
    ports = validate_ports(port_base)
    if reuse and not IMAGE_ID.fullmatch(reuse):
        raise ValueError("reuse requires an exact local sha256 image ID")
    head = source_head(root)
    base = state_dir(root, project)
    if (base / "state.json").exists():
        previous = read_state(root, project)
        if previous["ports"] != ports or previous["reuse_base_image"] != reuse:
            raise ValueError("existing snapshot configuration differs; use a new project name")
        print(json.dumps(previous, indent=2))
        return
    check_ports(ports)
    if run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
            "--format", "{{.ID}}"], capture=True).stdout.strip():
        raise ValueError("project already has containers; choose a new project name")
    if run(["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={project}",
            "--format", "{{.Name}}"], capture=True).stdout.strip():
        raise ValueError("project already has volumes; choose a new project name")
    base.mkdir(parents=True, exist_ok=False)
    os.chmod(base, 0o700)
    # Build context is never mounted into a running application. Runtime writes
    # stay in a separate source snapshot and project-scoped volumes.
    context = base / "build"
    context.mkdir()
    try:
        archive_source(root, context)
        (context / ".git_sha").write_text(head + "\n")
        if reuse:
            # Retain dependencies from an explicitly selected image, replace only
            # application source. This is CACHE_REUSE, never a cold/fresh build.
            (context / "docker/isolated/cache.Dockerfile").write_text(
                f"FROM {reuse}\nWORKDIR /app\n" +
                "RUN mv /app/node_modules /cached_node_modules && rm -rf /app && mkdir /app\n" +
                "COPY . /app\nRUN mv /cached_node_modules /app/node_modules\n" +
                metadata_block((context / "docker/Dockerfile").read_text()) + "\n")
        shutil.copytree(context, base / "source")
        secret_file = base / "secrets.env"
        descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            for name, size in [("SECRET_KEY_BASE", 64), ("POSTGRES_PASSWORD", 32), ("REDIS_PASSWORD", 32)]:
                output.write(f"{name}={secrets.token_hex(size)}\n")
        state = {
            "schema": 1, "project": project, "source_commit": head,
            "checkout": str(root), "ports": ports, "reuse_base_image": reuse,
            "dependency_mode": "CACHE_REUSE" if reuse else "FULL_BUILD",
        }
        (base / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    except BaseException:
        # Only remove the new directory exclusively created by this invocation.
        shutil.rmtree(base)
        raise
    print(json.dumps(state, indent=2))


def read_state(root, project):
    base = state_dir(root, project)
    state = json.loads((base / "state.json").read_text())
    expected = {"schema", "project", "source_commit", "checkout", "ports",
                "reuse_base_image", "dependency_mode"}
    if set(state) != expected or state["schema"] != 1:
        raise ValueError("unsupported state")
    if state["project"] != project or state["checkout"] != str(root):
        raise ValueError("state does not belong to this project/checkout")
    if state["source_commit"] != source_head(root):
        raise ValueError("snapshot HEAD changed; prepare a new project")
    if state["ports"] != validate_ports(state["ports"][0]):
        raise ValueError("invalid state ports")
    reuse = state["reuse_base_image"]
    if reuse is not None and not IMAGE_ID.fullmatch(reuse):
        raise ValueError("invalid cached image ID")
    if state["dependency_mode"] != ("CACHE_REUSE" if reuse else "FULL_BUILD"):
        raise ValueError("inconsistent dependency mode")
    secret_file = base / "secrets.env"
    if secret_file.is_symlink() or secret_file.stat().st_mode & 0o077:
        raise ValueError("secret file must be private (chmod 600)")
    return state


def compose(root, state, arguments):
    project = state["project"]
    base = state_dir(root, project)
    # Explicit -p/-f and computed structural values prevent ambient Compose
    # configuration or secrets.env from choosing a different project/source.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    for name in ("SECRET_KEY_BASE", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        environment.pop(name, None)
    environment.update({
        "CW_SOURCE": str(base / "source"), "CW_SECRET_FILE": str(base / "secrets.env"),
        "CW_BUILD_SOURCE": str(base / "build"),
        "CW_IMAGE": f"{project}:app-{state['source_commit'][:12]}",
        "CW_COMMIT": state["source_commit"],
        "CW_DOCKERFILE": "docker/isolated/cache.Dockerfile" if state["reuse_base_image"] else "docker/Dockerfile",
        "CW_DATABASE": project.replace("-", "_"),
        "CW_RAILS_PORT": str(state["ports"][0]), "CW_VITE_PORT": str(state["ports"][1]),
        "CW_MAIL_PORT": str(state["ports"][2]),
    })
    command = ["docker", "compose", "--project-name", project,
               "--env-file", str(base / "secrets.env"),
               "--file", str(root / "docker/isolated/compose.yaml"), *arguments]
    return run(command, cwd=root, env=environment)


def owned_ports(project):
    identifiers = run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                       "--format", "{{.ID}}"], capture=True).stdout.split()
    ports = set()
    for identifier in identifiers:
        mappings = json.loads(run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}",
                                   identifier], capture=True).stdout)
        for bindings in mappings.values():
            for binding in bindings or []:
                if binding["HostIp"] == "127.0.0.1":
                    ports.add(int(binding["HostPort"]))
    return ports


def metadata_regression(root, base_image, baseline):
    if not IMAGE_ID.fullmatch(base_image) or not SHA.fullmatch(baseline):
        raise ValueError("regression requires exact image ID and baseline commit")
    # Ensure a missing Git executable cannot produce a false baseline failure.
    run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "git", base_image, "--version"])
    old = metadata_block(git(root, "show", f"{baseline}:docker/Dockerfile"))
    new = metadata_block((root / "docker/Dockerfile").read_text())
    if old == new or "RUN git rev-parse HEAD > /app/.git_sha" not in old:
        raise ValueError("baseline must contain the real upstream Git metadata command")
    head = source_head(root)
    tags = []
    fixture = None
    try:
        with tempfile.TemporaryDirectory(prefix="cwdev-metadata-") as temporary:
            directory = Path(temporary)
            fixture = directory / "linked"
            git(root, "worktree", "add", "--detach", str(fixture), head)
            context = directory / "context"
            context.mkdir()
            shutil.copyfile(fixture / ".git", context / ".git")
            prefix = f"FROM {base_image}\nWORKDIR /app\nCOPY .git /app/.git\n"
            tag_prefix = f"cwdev-regression-{uuid.uuid4().hex[:12]}"

            def build(suffix, block, commit=None, expect=True):
                tag = f"{tag_prefix}:{suffix}"
                tags.append(tag)
                (context / "Dockerfile").write_text(prefix + block + "\n")
                command = ["docker", "build", "--network=none", "--pull=false", "--no-cache",
                           "--progress=plain", "-t", tag]
                if commit is not None:
                    command += ["--build-arg", f"CHATWOOT_SOURCE_COMMIT={commit}"]
                result = run([*command, str(context)], capture=True, check=False)
                if expect and result.returncode:
                    raise RuntimeError(result.stdout)
                return tag, result

            _, failure = build("baseline", old, expect=False)
            if failure.returncode == 0 or "not a git repository" not in failure.stdout:
                raise AssertionError("baseline did not fail specifically on the dangling worktree metadata")
            fixed, _ = build("fixed", new, head)
            actual = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat",
                          fixed, "/app/.git_sha"], capture=True).stdout.strip()
            if actual != head:
                raise AssertionError("fixed image metadata differs from real Git HEAD")
            for suffix, value in [("short", "abc"), ("nonhex", "g" * 40), ("long", "a" * 41)]:
                _, invalid = build(suffix, new, value, expect=False)
                if invalid.returncode == 0:
                    raise AssertionError("invalid SHA accepted")
            # Exercise default Docker behavior with a real, synthetic normal clone
            # metadata directory, never copy user Git configuration/credentials.
            normal = directory / "normal"
            normal.mkdir()
            git(normal, "init")
            git(normal, "-c", "user.name=Regression", "-c", "user.email=regression@example.invalid",
                "commit", "--allow-empty", "-m", "metadata fixture")
            normal_head = git(normal, "rev-parse", "HEAD")
            (context / ".git").unlink()
            shutil.copytree(normal / ".git", context / ".git")
            fallback, _ = build("fallback", new)
            actual = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat",
                          fallback, "/app/.git_sha"], capture=True).stdout.strip()
            if actual != normal_head:
                raise AssertionError("normal Git metadata fallback failed")
            print(json.dumps({"baseline": "EXPECTED_FAIL", "explicit_sha": "PASS",
                              "invalid_sha": "REJECTED", "normal_git_fallback": "PASS",
                              "source_commit": head, "baseline_commit": baseline,
                              "base_image": base_image}, indent=2))
            git(root, "worktree", "remove", str(fixture))
            fixture = None
    finally:
        if fixture is not None:
            # Remove only the registration created by this invocation, including
            # on failure after TemporaryDirectory removed its generated path.
            git(root, "worktree", "remove", "--force", str(fixture))
        for tag in tags:
            run(["docker", "image", "rm", tag], capture=True, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="cwdev-complete001")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--port-base", type=int, default=44440)
    setup.add_argument("--reuse-base-image")
    regression = commands.add_parser("metadata-regression")
    regression.add_argument("--base-image", required=True)
    regression.add_argument("--baseline", default=BASELINE)
    for name in ("plan", "config", "build", "up", "prepare-db", "test", "restart", "stop", "status", "smoke"):
        commands.add_parser(name)
    args = parser.parse_args()
    root = Path(git(Path(__file__).resolve().parent, "rev-parse", "--show-toplevel")).resolve()
    validate_project(args.project)
    if args.command == "prepare":
        prepare(root, args.project, args.port_base, args.reuse_base_image)
    elif args.command == "metadata-regression":
        metadata_regression(root, args.base_image, args.baseline)
    else:
        state = read_state(root, args.project)
        if args.command == "plan":
            print(json.dumps(state, indent=2))
        elif args.command == "config":
            compose(root, state, ["config", "--quiet"])
        elif args.command == "build":
            compose(root, state, ["--profile", "build", "build", "base"])
            image = f"{state['project']}:app-{state['source_commit'][:12]}"
            actual = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat",
                          image, "/app/.git_sha"], capture=True).stdout.strip()
            if actual != state["source_commit"]:
                raise ValueError("built image metadata does not match prepared Git HEAD")
            print(f"IMAGE_METADATA_PASS {actual}")
        elif args.command == "up":
            check_ports(state["ports"], owned_ports(state["project"]))
            compose(root, state, ["up", "-d", "--no-build", "rails", "sidekiq", "vite", "mailhog"])
        elif args.command == "prepare-db":
            compose(root, state, ["run", "--rm", "rails", "bundle", "exec", "rails", "db:chatwoot_prepare"])
        elif args.command == "test":
            compose(root, state, ["--profile", "test", "run", "--rm", "test", "bundle", "exec", "rails", "db:prepare"])
            compose(root, state, ["--profile", "test", "run", "--rm", "test", "bundle", "exec", "rspec",
                                  "spec/models/installation_config_spec.rb"])
            compose(root, state, ["run", "--rm", "--no-deps", "vite", "pnpm", "test",
                                  "app/javascript/shared/helpers/specs/DateHelper.spec.js",
                                  "app/javascript/widget/helpers/specs/urlParamsHelper.spec.js"])
        elif args.command == "restart":
            compose(root, state, ["restart", "rails", "sidekiq", "vite"])
        elif args.command == "stop":
            compose(root, state, ["stop"])
        elif args.command == "status":
            compose(root, state, ["ps"])
        elif args.command == "smoke":
            url = f"http://127.0.0.1:{state['ports'][0]}/app/login"
            with urllib.request.urlopen(url, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"unexpected HTTP {response.status}")
            print(f"HTTP_SMOKE_PASS {url}")


if __name__ == "__main__":
    main()

