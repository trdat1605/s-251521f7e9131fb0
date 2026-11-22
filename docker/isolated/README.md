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

One nonruntime developer-rule pointer is explicitly omitted from generated
archives: .windsurf/rules/chatwoot.md, only when it is a symlink with the pinned
upstream target ../../AGENTS.md. This upstream link is internal, not unsafe or
dangling. The local Python 3.10.12 tarfile data filter resolves its linkname
against the extraction root instead of the symlink's parent, incorrectly
reporting it outside the destination. The omission avoids that compatibility
defect without disabling or replacing the data filter. A changed type or target
at this exact path requires review; other paths are filtered normally.
All extracted members still use filter="data", other external links remain
rejected, normal internal links remain supported, and root AGENTS.md is retained.
state.json records the exact archive_omission_policy; no host AGENTS is followed.

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
The private node_modules volume is seeded from the image. The scoped entrypoint
checks that gems or the prebuilt Vite executable are available; it never runs
pnpm install during service startup. Missing dependencies fail with a build
instruction. Dependency changes require a new image/project. It does not perform
the upstream Vite forced reinstall, Rails PID deletion or shared cache deletion.

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
The CLI retains the exact image ID as provenance. At build time it creates an
owned random cwdev-base-...:selected local tag and verifies that tag resolves
to the selected ID before and after the build. The Dockerfile uses that named
reference, because a local image ID is not a registry digest and BuildKit may
interpret FROM sha256:... as a repository/tag. Only the generated temporary tag
is removed, after an ID check; existing lab tags are never retagged or removed.
If a temporary tag changes identity, cleanup leaves it untouched and fails.
The caller's base image must already have an independent named tag; untagged
images are rejected before tag creation. Cleanup also requires another tag still
resolving to that exact base image ID. If the independent tag disappears or
changes identity, cleanup fails and retains the temporary tag to preserve the
pre-existing image object. No permanent tag is added automatically. Concurrent
external Docker mutations are not isolated; stop them during this operation.
Owned image-tag removal uses --no-prune to retain any untagged parent images.
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

This regression reads the baseline Git object
caab07a8f00032d5205b5a5d7e2eb0ba2f3d2d82. A shallow clone may not contain it.
Check it before the regression; if absent, fetch that exact public upstream
commit, without modifying the checkout or application data:

    git cat-file -e caab07a8f00032d5205b5a5d7e2eb0ba2f3d2d82^{commit}
    git fetch https://github.com/chatwoot/chatwoot.git caab07a8f00032d5205b5a5d7e2eb0ba2f3d2d82

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

foundation-ci.yml runs these Python and Docker regressions on every pull
request and manual invocation. It has contents:read only, no secrets, publish,
push or schedule trigger. PR frontend checks (optional on manual runs) install locked
dependencies and run existing focused Vitest tests. Enabling this workflow and
disabling inherited publish/scheduled workflows is a separate reviewed repository
operation. File existence is not evidence of CI execution or successful runtime
reproduction.

## Disposable hosted-runner lifecycle verification

The CI isolated-runtime job invokes:

    python3 -B docker/isolated/verify_runtime.py

This helper is intended for a disposable Linux runner with Docker, Compose,
Python, Git, network access and at least 15 GiB free before allocation. It uses
the fixed project cwdev-ci-runtime and ports 44440–44442. Do not invoke it on a
shared development host or alongside another run with those identities. It
performs a full build, prepares synthetic development/test databases, checks
API/login/Vite/Mailhog HTTP responses, runs focused application tests, restarts
its services, dumps the synthetic database in memory and restores it into a
new database, then checks selected row counts and repeated preparation.

The final FOUNDATION_RUNTIME_EVIDENCE JSON binds the result to actual Git HEAD.
A failed step or failure to stop owned services exits nonzero. It stops only
its project; it does not remove volumes or images, which the disposable hosted
runner discards. A passing run proves only these checks on that runner. It
does not verify interactive browser login, all features, a production deployment
or recovery of real customer data. Use the manual workflow above for an approved
local project and record its separate evidence.

## Sources and ownership

- [Chatwoot Docker development setup](https://developers.chatwoot.com/contributing-guide/environment-setup/docker): base-first build, database preparation and startup.
- [Pinned upstream Dockerfile](https://github.com/chatwoot/chatwoot/blob/caab07a8f00032d5205b5a5d7e2eb0ba2f3d2d82/docker/Dockerfile#L77-L90): source copy and original Git metadata command.
- [Git linked-worktree metadata](https://git-scm.com/docs/git-worktree#_details): separate common/private Gitdirs and .git pointer.
- [Docker build contexts](https://docs.docker.com/build/concepts/context/#filesystem-contexts): files available to the build.
- [Compose project names](https://docs.docker.com/compose/how-tos/project-name/): environment namespace; ports/bind mounts still require explicit isolation.
- [BuildKit image-ID compatibility issue](https://github.com/moby/buildkit/issues/2204): local image IDs in FROM can be interpreted as repository/tag references; a verified owned named tag avoids that syntax.

Application issue: https://github.com/trdat1605/s-251521f7e9131fb0/issues/2.
Control task: [COMPLETE-001](https://github.com/trdat1605/r-61974bac4c68fe3c/blob/codex/complete-foundation/tasks/COMPLETE-001/task.json)
in the separate coordination repository; [completion checkpoint](https://github.com/trdat1605/r-61974bac4c68fe3c/blob/codex/complete-foundation/docs/development/completion.md).
These are working-branch links, not acceptance records; final review receipts
must name immutable control and app commit SHAs separately.
Runtime acceptance, browser checks, backup/restore evidence, exact-commit review,
PR integration and merge/release decisions remain separate recorded steps.
