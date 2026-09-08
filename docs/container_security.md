# Container Security

## Runtime boundary

The checked-in Compose topology uses one locked platform image for the API, dashboard, durable-job
worker, migration, and database-bootstrap commands. The data-only collector target remains
separate so the live market-data process does not contain the Alpaca trading SDK. A third execution
target isolates the default-deny paper gate from strategy code. All three targets use
a digest-pinned Wolfi base and uv image, install from `uv.lock`, and run as numeric UID/GID `10001`.
Python 3.11.16 and SQLite 3.53.4 are exact APK package pins from the signed Wolfi
repository. Transitive OS dependencies are resolved from that repository and recorded
in each build SBOM; their versions can change between rebuilds and must pass the gate.
The glibc runtime supports the existing locked manylinux wheels. Global Python
installers and bundled installer wheels are removed from final stages.
Compiler and dependency-installation tooling remains in build stages.

Application services use a read-only root filesystem, an explicit `/tmp` tmpfs, dropped Linux
capabilities, `no-new-privileges`, bounded restarts, PID/CPU/memory limits, and narrowly scoped
volumes. Application code and the virtual environment remain root-owned; `/app` is owned by the
runtime identity but mode `0555`, while writable runtime and secret directories are explicit. The
PostgreSQL service has a dedicated durable volume and private network. The one-shot
`database-bootstrap` job is the only application image process receiving cluster-administrator and
role-bootstrap secrets; it has no restart loop and no Alpaca or broker secret. The `migrate` job
then runs with only the migration-role password.

Compose mounts secret files per service. It does not inject secret values into environment
variables. The container entrypoint creates only a process-owned `0600` database-URL file in the
ephemeral `/tmp` filesystem, removes its internal role selector before executing the service, and
never renders the URL. Operators must create the local infrastructure files with
`aqa secrets bootstrap-local`; provider files are supplied only when an explicit provider profile
is selected.

The dashboard never mounts the operator token. At control-API startup, the entrypoint loads that
base token through the owner-private file boundary, derives a domain-separated HMAC bearer, and
atomically replaces a `0600` file in the dedicated `dashboard-auth` volume. The dashboard mounts
that volume read-only and receives only the derived bearer. The API recognizes it as read-only:
authenticated reads are permitted and every mutation returns `403` without creating a job or
changing a latch. Invalid ownership, permissions, symlinks, stale temporary state, or an unexpected
token source stops startup without rendering either credential.

Some local Compose implementations warn that `uid`, `gid`, and `mode` on file-backed secrets are
not enforced. The runtime does not compensate by accepting permissive files: it requires the
presented secret to be owned by numeric UID `10001` with mode `0400` or `0600`. Before deployment,
verify the chosen container secret backend presents those attributes; otherwise startup fails
closed. Docker administrators and host root remain able to inspect volumes and are part of the
deployment trust boundary.

## Network boundary

The default topology is offline. PostgreSQL, the API, and the durable-job worker use only internal
database/control networks. The dashboard shares only the internal control network with the API.
Only the API and dashboard publish ports, both on `127.0.0.1`. The `db-debug` profile adds a
bounded, credential-free TCP relay published on loopback; it is absent by default. PostgreSQL URLs
may use plaintext only for loopback or the exact internal Compose hostname `postgres`; every other
hostname requires `sslmode=verify-full`.

`market-data-live` and `paper-execution-worker` are disabled unless the `market-data` or `paper`
profile is selected. They use separate egress networks and disjoint credentials. The tracked paper
profile and Compose environment both leave submission disabled and acknowledgement absent. The
paper loop records default-deny decisions and health, but cannot authorize submissions while Main
AI approval remains unimplemented and frozen. Its adapter contracts have only fake-provider evidence.

The default graph includes offline market-data, scheduler, strategy, and fake-execution workers.
They run bounded durable cycles through `platform/worker_runtime.py` and `platform/service_cycles.py`,
with role-scoped database access, health markers, leases, and shutdown handling. Durable jobs and
outbox delivery run in `job-worker` with the bounded control database role. Unit, PostgreSQL and
container probes validate these boundaries; a continuously deployed operator-host service graph
has not been externally validated.

Compose network segmentation is not an outbound firewall. A process attached to either provider
egress network can attempt other Internet destinations. Production operators remain responsible
for host or cloud egress policy, DNS policy, container-runtime patching, and preventing untrusted
users from controlling Docker.

## Build and scan policy

CI builds without publishing images from pull requests, verifies the numeric runtime user,
generates SPDX image and CycloneDX Python SBOM artifacts, and runs Trivy against high and critical
OS/library findings. Each matrix job retains its JSON findings even when that gate fails.
Gitleaks, pip-audit, Bandit, architecture tests, secret-redaction tests, and
CodeQL are separate read-only gates. These workflows contain no Alpaca credentials.

A vulnerability may be temporarily excepted only through a reviewed, time-bounded repository
change recording the advisory ID, affected package/image, exploitability in this deployment,
compensating control, owner, upstream tracking link, and expiration date. There are no checked-in
exceptions. SBOMs are generated artifacts; they are not committed or published as releases.

Configuration tests prove the declared topology and gates. They do not prove that a particular
host kernel, Docker daemon, registry artifact, scan database, or deployed secret-mount ownership
behaves correctly; those controls remain deployment verification responsibilities.
