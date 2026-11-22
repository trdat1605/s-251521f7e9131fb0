#!/usr/bin/env python3
"""Exercise only the isolated synthetic project; emit sanitized, ref-bound evidence."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).with_name("bootstrap.py"))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def main():
    root = Path(bootstrap.git(Path(__file__).resolve().parent, "rev-parse", "--show-toplevel")).resolve()
    project = "cwdev-ci-runtime"
    bootstrap.validate_project(project)
    evidence = {"status": "IN_PROGRESS", "project": project,
                "source_commit": bootstrap.source_head(root), "steps": [],
                "limits": ["Synthetic isolated hosted-runner verification; no production acceptance",
                           "Focused RSpec/Vitest only; no full application suite or security certification"]}
    state = None
    secret_values = []

    def call(command, label, binary=False, input_data=None, env=None):
        result = subprocess.run(command, cwd=root, capture_output=True, text=not binary,
                                input=input_data, timeout=1800, env=env)
        if result.returncode:
            text = (result.stdout + result.stderr) if not binary else result.stderr.decode(errors="replace")
            for value in secret_values:
                text = text.replace(value, "<redacted synthetic secret>")
            print("FAILED_STEP " + label + "\n" + text[-5000:])
            raise RuntimeError(label)
        evidence["steps"].append({"name": label, "status": "PASS", "exit_code": 0})
        return result.stdout

    def boot(command, label):
        return call([sys.executable, "-B", "docker/isolated/bootstrap.py", "--project", project, command], label)

    def owned_container(service):
        ids = subprocess.check_output(["docker", "ps", "-aq", "--no-trunc", "--filter",
                                       "label=com.docker.compose.project=" + project, "--filter",
                                       "label=com.docker.compose.service=" + service], text=True).split()
        if len(ids) != 1:
            raise RuntimeError("Expected one owned service " + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        if item["Config"]["Labels"]["com.docker.compose.project"] != project:
            raise RuntimeError("Project identity mismatch")
        return ids[0]

    def wait_http(port, path):
        last = "NOT_READY"
        for _ in range(90):
            try:
                with urllib.request.urlopen("http://127.0.0.1:" + str(port) + path, timeout=10) as response:
                    content = response.read()
                    if response.status == 200:
                        return {"port": port, "path": path, "http": 200, "bytes": len(content),
                                "sha256": hashlib.sha256(content).hexdigest()}
            except Exception as error:
                last = type(error).__name__
            time.sleep(2)
        raise RuntimeError("HTTP readiness " + path + " " + last)

    def counts(pg, database):
        query = ("SELECT 'accounts=' || count(*) FROM accounts UNION ALL "
                 "SELECT 'users=' || count(*) FROM users UNION ALL "
                 "SELECT 'installation_configs=' || count(*) FROM installation_configs;")
        return call(["docker", "exec", pg, "psql", "-U", "postgres", "-d", database,
                     "-At", "-c", query], "synthetic_counts_" + database).strip().splitlines()

    try:
        free_bytes = shutil.disk_usage(root).free
        evidence["capacity"] = {"free_bytes": free_bytes, "minimum_bytes": 15 * 1024 ** 3}
        if free_bytes < 15 * 1024 ** 3:
            raise RuntimeError("Insufficient runner capacity: require 15 GiB free before allocation")
        call([sys.executable, "-B", "docker/isolated/bootstrap.py", "--project", project,
              "prepare", "--port-base", "44440"], "prepare")
        state = bootstrap.read_state(root, project)
        private = bootstrap.state_dir(root, project)
        secret_values = [line.split("=", 1)[1] for line in (private / "secrets.env").read_text().splitlines()]
        boot("config", "compose_config")
        boot("build", "full_app_image_build_and_sha")
        boot("prepare-db", "synthetic_database_prepare")
        boot("up", "start_isolated_services")
        evidence["http_before"] = [wait_http(44440, "/api"), wait_http(44440, "/app/login"),
                                   wait_http(44441, "/vite-dev/@vite/client"), wait_http(44442, "/")]
        pg = owned_container("postgres")
        initial = counts(pg, "cwdev_ci_runtime_dev")
        boot("test", "focused_application_tests")
        # Additional existing endpoint/session tests use only the isolated test service.
        original_run = bootstrap.run
        try:
            def capture(args, **kwargs):
                return call(args, "focused_api_and_session_specs", env=kwargs.get("env"))
            bootstrap.run = capture
            bootstrap.compose(root, state, ["--profile", "test", "run", "--rm", "test", "bundle", "exec", "rspec",
                                            "spec/controllers/api_controller_spec.rb", "spec/config/session_store_spec.rb"])
        finally:
            bootstrap.run = original_run
        boot("restart", "restart_application_services")
        evidence["http_after_restart"] = [wait_http(44440, "/api"), wait_http(44441, "/vite-dev/@vite/client")]
        if counts(pg, "cwdev_ci_runtime_dev") != initial:
            raise RuntimeError("Synthetic counts changed after restart")
        backup = call(["docker", "exec", pg, "pg_dump", "-U", "postgres", "-d", "cwdev_ci_runtime_dev",
                       "--format=custom", "--no-owner"], "backup_synthetic_database", binary=True)
        call(["docker", "exec", pg, "createdb", "-U", "postgres", "cwdev_ci_runtime_restore"], "create_restore_database")
        call(["docker", "exec", "-i", pg, "pg_restore", "-U", "postgres", "-d", "cwdev_ci_runtime_restore",
              "--no-owner", "--exit-on-error"], "restore_synthetic_backup", binary=True, input_data=backup)
        restored = counts(pg, "cwdev_ci_runtime_restore")
        if restored != initial:
            raise RuntimeError("Restored counts mismatch")
        evidence["restore"] = {"counts_before": initial, "counts_after": restored,
                               "dump_sha256": hashlib.sha256(backup).hexdigest(), "dump_bytes": len(backup)}
        boot("prepare", "repeat_prepare_idempotent")
        evidence["status"] = "PASS"
        evidence["exit_code"] = 0
    except Exception as error:
        evidence.update(status="FAIL", exit_code=1, failed_step=str(error))
    finally:
        # Stop only owned services. Hosted runner destroys its own remaining resources.
        if state is not None:
            try:
                boot("stop", "stop_owned_services")
            except Exception:
                evidence["cleanup"] = "FAIL"
                evidence["status"] = "FAIL"
                evidence["exit_code"] = 1
                evidence.setdefault("failed_step", "stop_owned_services")
        print("FOUNDATION_RUNTIME_EVIDENCE " + json.dumps(evidence, sort_keys=True))
    return evidence["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
