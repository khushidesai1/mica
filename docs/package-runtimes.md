# Python package runtimes

Mica workers expose `ensure_packages` when `AGENT_RUNTIMES_ENABLED=true`. Registration is lazy:
no package is installed until Claude calls the tool. Ordinary conversational answers do not
start a worker or install packages. The same registration and policy settings reach CPU and GPU
workers; installing a package does not itself allocate a GPU.

The tool resolves public Python requirements into a separate, hash-locked environment. It first
tries an overlay constrained to the existing science environment, then an isolated environment
when the existing versions conflict. Successful runtimes are validated offline and saved under
`projects/_toolkit` in the artifact bucket. A compatible request from another project restores the
saved runtime. Newly installed packages and any pure-Python source builds appear in the setup
notice; a restore does not repeat that notice.

Pass a complete dependency set as up to 100 package/version requirements in one call. Pin the
scientifically required versions; a transfer failure is not a reason to substitute versions.

## Source dependencies and policy

The trusted mirror uses PyPI metadata and `files.pythonhosted.org` artifacts. The resolver remains
wheel-only. If resolution identifies a dependency without a usable wheel, the installer may fetch
an eligible SHA256-pinned source archive, build it in the existing networkless, credential-free
sandbox, and retry resolution once with the resulting wheel. Build requirements themselves must
have wheels. Source archives containing native code, unsafe paths or links are refused. The output
must be a `py3-none-any` wheel with matching name, version and pure-Python metadata. Packages that
need a compiler or platform-specific build require an admin-built runtime.

Source files follow the same minimum age as wheels: seven days by default. Agent-provided Git
requirements are refused; immutable sources in the reviewed toolkit catalog remain supported.
The default denylist covers cloud and credential SDKs, remote shell/process wrappers and specialized
network clients. Common scientific dependencies such as requests, fsspec and pexpect remain
available inside the existing offline analysis sandbox. An unrequested optional cloud extra does
not block a package's core install; selecting that extra is refused when it brings in a denied
package. The resolved lock and restored runtime are checked again against the denylist.

A failed installation records a request-specific **Package setup** incident through the worker's
existing storage receipt and controller polling path. The tool returns its error to Claude so the
worker can explain it or continue another part of the task. Policy refusals are recomputed rather
than cached. Transient download failures are also retryable, including failures saved by older
workers. HTTP transfers allow three retries and a five-minute read timeout. Repeating the same
dependency set after a transient failure does not consume another distinct-runtime slot; each
set still has at most three setup attempts per turn.
The mirror preserves an upstream package-index HTTP 404 so the resolver reports an absent
distribution. Real registry/network failures remain retryable 502 responses. This distinction
does not change package permissions or allow external URLs. Native tools are not installed by
`ensure_packages`: the pinned Proseg CLI is shipped with the worker image and advertised in
the environment catalog; `proseg`/`proseg-py` are not PyPI install targets at this pin.
Repeatable installation failures expire after 24 hours; changing the policy version or relevant limits
also invalidates the old failure key. A failure incident records investigation, not proof that the
package installation later recovered.

## Rollout and limits

Release matching API, CPU worker and GPU worker application code before enabling the runtime
setting on `mica-staging`. Preserve the current science environments, mailbox references and other
runtime settings. Use the existing Terraform runtime environment map for the reviewed operator
configuration; do not enable an older worker image's package policy.

The relevant settings are:

| Setting | Default |
| --- | --- |
| `AGENT_RUNTIMES_ENABLED` | `false` until operator rollout |
| `AGENT_RUNTIME_MIN_AGE_DAYS` | `7` |
| `AGENT_RUNTIME_MAX_SOURCE_BUILDS` | `5` |
| `AGENT_RUNTIME_FAILURE_TTL_HOURS` | `24` |
| `AGENT_RUNTIME_MAX_GB` | `24` GiB per installed environment |
| `AGENT_RUNTIME_MAX_SCRATCH_GB` | `48` GiB for setup scratch, including the installer cache |
| `AGENT_RUNTIME_MAX_DOWNLOAD_GB` | `24` GiB transferred per setup |
| `AGENT_RUNTIME_MIN_FREE_DISK_GB` | `20` GiB reserved for analysis |
| `AGENT_RUNTIME_MAX_NEW_PER_TURN` | `3` |
| `AGENT_RUNTIME_TIMEOUT_SECONDS` | `2700` (45 minutes) |
| `AGENT_RUNTIME_MAX_LOCAL_GB` | `120` GiB total local runtime storage, including setup |
| `AGENT_RUNTIME_DENYLIST` | Seeded in `analysis.config.Settings` |

Capture and explicitly retain the seeded denylist in the deployed environment. Check it against
the intended scientific dependency resolution before rollout. Roll back enablement with
`AGENT_RUNTIMES_ENABLED=false`; saved runtime data remains inert.

The deployment gate uses one bounded CPU worker to install a small wheel and a pure-Python source
dependency through the real MCP handler, validate offline, save to temporary storage and restore
from a separate manager context. It also checks a denied package and removes the exact smoke
namespace afterward. This verifies worker NAT egress, source isolation and toolkit publication
together without a GPU job or scientific training. A newer compatible scvi-tools install, one-epoch
synthetic training and reuse from another project's private thread remain owner acceptance checks;
the small deployment fixture does not prove them. Record installed and setup sizes when tuning
these allowances. Unit tests with fakes do not prove live
network or storage permissions.

This feature does not add check-only mode, CUDA-driver matching or a new runtime browser. Running
an installed runtime on a GPU still requires the separately enabled GPU execution tool and an
approved GPU worker.

The mirror keeps concurrent requests for a wheel on one verified download. It finishes or
cancels active download handlers before deleting setup scratch. Downloaded artifacts are still
validated before the installer receives them. The larger allowances do not change source policy,
hash checks, credential isolation, GPU approval, or the worker's approved execution deadline.
