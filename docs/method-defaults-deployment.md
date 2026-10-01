# Standard method defaults — 29 September 2026

The latest Louvain task (`task_d22db319c98e488184743c713ec18a2c`) passed its current
unit/smoke checks, then paused in candidate review. The generated code used custom
reciprocal-distance graph weights and union symmetrization. The reviewer treated those
implementation choices as missing user authorizations. The older prompts also required
explicit approval for every output-affecting default, contradicting routine technical autonomy.

The shared method-authority policy now applies to initial and follow-up planning, tool
selection, coding, candidate review and result review. Requesting a method authorizes its
standard implementation and documented defaults for unspecified arguments. Explicit user
settings remain authoritative. An unsolicited custom graph should be repaired to use the
standard package workflow; the user should not be asked to approve the invention.

Structured `decision_scope` distinguishes implementation work, method defaults and substantive
research design. Planner and development approval gates require the research-design scope;
output sensitivity alone is insufficient. Effective manifests and resolved invocations preserve
default provenance without claiming explicit user confirmation. Old plan fields serialize
unchanged so existing authority hashes remain compatible with the new scope field.

For this task, the intended implementation uses Scanpy's standard neighbors/Louvain workflow,
preserving the requested representation, neighbor count, metric, resolution and seed. It does
not force an extra directed-to-undirected conversion or invent edge weighting. API behavior
must be verified against the installed version. See the
[Scanpy Louvain reference](https://scanpy.readthedocs.io/en/1.11.x/generated/scanpy.tl.louvain.html).

Validation: 150 tests passed, three skipped; lint and type checks passed. The isolated deployment
source also passed the full suite. Regression coverage includes initial parameter gating, legacy
tool review, coder amendments, candidate review, result review, provenance and legacy serialization.
The final UI-baseline refresh changed only the UI template; its eight API tests and lint passed.

An optional live-model regression test was rejected by automatic approval review because it would
send project prompts through the staging API credential. It was not executed or retried indirectly.
No full scientific analysis was rerun and no task or compute approval was changed.

Deployment details are recorded after rollout in `.pistachio/deployments/method-defaults-20260929.json`.

Staging is now on `pistachio-staging-00056-wuw` at 100% traffic. Build
`7006ba64-79c2-43d4-b104-1729629bcb24` succeeded. Preview and live health/schema checks
passed, runtime settings were unchanged, and no ERROR-level logs were returned during
the rollout check. The final UI template is preserved byte for byte from build
`3fcdc07a-6094-4f55-807c-c3a5f2091e7c`. Rollback revision: `pistachio-staging-00054-get`.
