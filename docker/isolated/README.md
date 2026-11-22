# Isolated development snapshot

A linked Git worktree has a .git pointer outside its source directory. The upstream
Dockerfile copied that pointer into an image and ran git rev-parse HEAD there.
The image cannot resolve the host metadata. docker/Dockerfile now accepts
CHATWOOT_SOURCE_COMMIT, validates exactly 40 lowercase hexadecimal characters,
and retains normal Git-directory discovery when the argument is omitted.

This wrapper derives the argument from actual Git HEAD. It rejects staged,
unstaged and untracked nonignored changes. Commit the reviewed implementation
before running it. It archives tracked source only, rejects tracked local adapters
and non-example environment files, and never copies the checkout's .git,
ignored .env or .codex contents. The generated build context and runtime source
snapshot are separate: application writes cannot change the build context or
the original checkout.

## Requirements and boundaries

Use Linux, Git, Python with tarfile's data extraction filter (verified locally on
Python 3.10.12; Python 3.12+ also provides it), Docker and Docker Compose v2.
Dependency images/packages require network access on first build. Use a separate
worktree per task. The Python boundary tests require only the standard library.

Every project must have a unique cwdev- name. Protected lab/course identities are
rejected. Fresh preparation rejects pre-existing containers/volumes with the same
Compose project label and occupied selected ports. Example host ports are
44440/44441/44442, bound only on 127.0.0.1. Postgres and Redis publish no host ports.
The development and test databases differ; tests also use a separate Redis DB
index and separate storage/tmp volumes. No volume is external.

Only .codex/isolated/PROJECT is generated, including a mode-600 secrets.env
containing fresh random values. Do not print resolved Compose configuration,
commit that directory, copy existing lab credentials/data, or use an existing
runtime's volume. The CLI provides fixed operations; it accepts no arbitrary
Compose flags, source SHA or Compose-file override. Ambient Compose settings and
structural interpolation variables cannot select another project through it.

## Full build and startup

Run from the application checkout after the runtime plan has been reviewed:

    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 prepare --port-base 44440
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 plan
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 config
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 build
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 prepare-db
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 up
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 smoke
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 test
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 restart
    python3 -B docker/isolated/bootstrap.py --project cwdev-complete001 smoke

The build command builds the upstream-derived development base image first.
Rails, Sidekiq and Vite use that same image and /gems dependencies. After building,
a disposable container reads /app/.git_sha and must match recorded Git HEAD.
The node_modules volume is seeded from the image. The scoped entrypoint checks
gems or performs a frozen-lockfile frontend installation without the upstream
Vite forced reinstall, Rails PID deletion or shared cache deletion.

The URL is http://localhost:44440/app/login and Mailhog is localhost:44442.
smoke checks an HTTP login-page response; it does not prove interactive login,
realtime, product acceptance or browser performance. Wait for startup readiness
before invoking smoke. test prepares only the test database, then runs existing
InstallationConfig RSpec, DateHelper and URL-parameter Vitest tests. These are
focused checks, not the full application suite.

prepare is idempotent for the same source/configuration and retains secrets/data.
up permits its own already-published ports and can be run again. A changed source
HEAD or configuration requires a new project name. status shows only that project;
stop stops its services without deleting volumes. There is no automatic prune,
down -v, database reset, migration of an existing lab, or deployment operation.

## Optional dependency reuse

For a constrained local verification, prepare a different project with
--reuse-base-image sha256: followed by an exact 64-hex local image ID:

    python3 -B docker/isolated/bootstrap.py --project cwdev-cached001 prepare --port-base 44443 --reuse-base-image sha256:REPLACE_WITH_64_HEX_ID

The selected image must already contain compatible /gems and /app/node_modules.
The generated Dockerfile retains those dependencies, removes inherited /app
source/adapters inside the image, copies the clean source archive and writes
verified source metadata. This path is recorded as CACHE_REUSE. It proves neither
a cold build nor that a new machine can fetch/install every dependency. The
default FULL_BUILD runs the full Dockerfile; Docker may still reuse its normal
layer cache. Record image IDs, package/network dependencies and actual build
outcome separately. FULL_BUILD and CACHE_REUSE are different evidence.

The procedure is intended to be repeatable. It does not promise bit-identical
images: upstream still uses mutable image tags, unpinned Alpine packages and
network dependency installation.

## Real Docker regression

First run Python boundaries:

    python3 -B -m unittest discover -s docker/isolated -v

After review, supply an exact local image ID containing Git (no host mounts):

    python3 -B docker/isolated/bootstrap.py metadata-regression --base-image sha256:REPLACE_WITH_64_HEX_ID

The harness reads the actual metadata block from the pinned upstream Dockerfile
and from the fixed Dockerfile. It creates a real temporary linked worktree,
copies only its dangling .git pointer into a tiny Docker context, and requires
the baseline build to fail specifically with Git's repository error. The fixed
build must write real HEAD correctly. Malformed short, nonhex and long arguments
must fail. A synthetic ordinary Git metadata directory tests the normal-clone
fallback without copying user Git config/credentials. Generated fixture worktree
registration and uniquely tagged test images are removed; shared images and
unrelated worktrees/containers/volumes are retained.

foundation-ci.yml runs these Python and Docker regressions on matching pull
requests and manual invocation. It has contents:read only, no secrets, publish,
push or schedule trigger. PR frontend checks (optional on manual runs) install locked
dependencies and run existing focused Vitest tests. Enabling this workflow and
disabling inherited publish/scheduled workflows is a separate reviewed repository
operation. File existence is not evidence of CI execution or successful runtime
reproduction.

## Sources and ownership

- [Chatwoot Docker development setup](https://developers.chatwoot.com/contributing-guide/environment-setup/docker): base-first build, database preparation and startup.
- [Pinned upstream Dockerfile](https://github.com/chatwoot/chatwoot/blob/caab07a8f00032d5205b5a5d7e2eb0ba2f3d2d82/docker/Dockerfile#L77-L90): source copy and original Git metadata command.
- [Git linked-worktree metadata](https://git-scm.com/docs/git-worktree#_details): separate common/private Gitdirs and .git pointer.
- [Docker build contexts](https://docs.docker.com/build/concepts/context/#filesystem-contexts): files available to the build.
- [Compose project names](https://docs.docker.com/compose/how-tos/project-name/): environment namespace; ports/bind mounts still require explicit isolation.

Application issue: https://github.com/trdat1605/s-251521f7e9131fb0/issues/2.
Control task: tasks/COMPLETE-001/task.json in the separate coordination repository.
Runtime acceptance, browser checks, backup/restore evidence, exact-commit review,
PR integration and merge/release decisions remain separate recorded steps.
