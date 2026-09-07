# Magy — Repository Audit

**Repository:** `https://github.com/Aeowun/Magy.git`  
**Branch audited:** `magy-current-work`  
**Audit date:** 2026-09-06  
**Method:** direct inspection of the public GitHub tree and raw checked-in source.  
**Repository state:** public; the repository page reports 27 commits on the audited branch.

---

## 0. Audit scope and evidence standard

This is a fresh audit of the repository itself, not a review of the prior coding-agent transcript.

The GitHub tree was enumerated first. Core source files were then inspected from their raw checked-in contents. Findings are tied to exact source line ranges wherever the raw file was available. No source-level claim below is based only on what the agent said it changed.

### Execution limitation

Git transport/clone was unavailable from the execution environment, so I did not run the repository's build or test suite independently. Runtime observations are therefore based on checked-in source, not a fresh local build.

The audit treats every tracked source/config/script file as in scope. For files where a complete raw body was not available through the web fetch path, the report records inventory scope without inventing line-by-line conclusions.

---

# 1. Repository inventory

The audited root contains:

```text
magy-app/
magy-cli/
magy-core/
magy-ui/
.gitignore
CHANGELOG.md
Cargo.lock
Cargo.toml
LICENSE
README.md
last_request.json
last_response.json
launch-magy.bat
magy-app.err
magy-app.log
magy-shell.bat
magy-shell.ps1
```

## `magy-core/src/application/`

```text
approval.rs
context_assembly.rs
coordinator.rs
execution.rs
mod.rs
planning.rs
project_lifecycle.rs
reasoning.rs
snapshot.rs
task_lifecycle.rs
tool_execution.rs
verification_runner.rs
```

## `magy-core/src/domain/`

```text
agent.rs
mod.rs
model.rs
project.rs
tool.rs
```

## `magy-core/src/infrastructure/`

```text
command.rs
filesystem.rs
mod.rs
model.rs
```

## `magy-core/src/`

```text
boundary.rs
lib.rs
```

## Application / CLI / UI

```text
magy-app/src/main.rs
magy-cli/src/main.rs
magy-ui/index.html
magy-ui/app.js
magy-ui/style.css
```

---

# 2. Architecture actually present

The implementation is materially more sophisticated than a simple LLM wrapper. The checked-in core has distinct Application, Domain, and Infrastructure layers, typed tool requests, a project-root filesystem boundary, deterministic approval policy, verification, snapshots, and a Planner/Executor split.

The root README states the central policy as:

> “The AI decides what it wants to do. The runtime decides what it is allowed to do.”

That principle is visible in the implementation: the model emits typed requests, while runtime code classifies and executes them.

However, the runtime currently has **two overlapping lifecycle/state representations**:

1. `Agent::State` in `domain/agent.rs`
2. `RunState` in `domain/model.rs`

The second state machine is more granular, but both participate in execution. This duplication is one of the highest-risk architectural areas because transition ownership is distributed across coordinator, execution, and runtime state code.

---

# 3. Critical findings

## C-01 — `run_execution_cycle()` can create an `AwaitingPlan` state that it does not handle

**Severity: CRITICAL**

`magy-core/src/application/execution.rs`, lines **56-66**:

```rust
if trace.run.state() == &RunState::Starting {
    trace.run.request_plan();
}
if trace.run.state() == &RunState::Planning
    || trace.run.state() == &RunState::AwaitingApproval
    || trace.run.state() == &RunState::Recovering
    || trace.run.state() == &RunState::Stalled
    || trace.run.state() == &RunState::ExecutingTask
{
    trace.run.await_model();
}
```

The first branch explicitly performs:

```text
Starting -> request_plan() -> AwaitingPlan
```

The second branch does not include `AwaitingPlan`.

At the same time, `magy-core/src/application/coordinator.rs`, lines **39-44**, already performs `trace.start(...)` followed by `trace.run.request_plan()`.

The `RunState` transition table in `magy-core/src/domain/model.rs`, lines **214-240**, also explicitly supports:

```text
Starting + RequestPlan -> AwaitingPlan
Starting + BeginTask  -> ExecutingTask
```

This demonstrates a genuine ownership conflict rather than a UI-only defect.

### Correct architectural direction

`coordinator.rs` should own the high-level planning lifecycle. `run_execution_cycle()` should receive an active task/execution state and should not silently initiate planning itself.

The regression test should enforce both flows independently:

```text
Starting
 -> AwaitingPlan
 -> Planning
 -> BeginTask
 -> ExecutingTask
```

and:

```text
ExecutingTask
 -> AwaitingModel
 -> ExecutingTool
 -> AwaitingApproval / AwaitingVerification / completion
```

---

## C-02 — Approval path classification and filesystem path resolution are separate algorithms

**Severity: HIGH**

`approval.rs` classifies paths with `is_within_boundary(root, &root.join(path))`.

`filesystem.rs` independently calls `resolve_and_validate(root, path)` before execution.

`boundary.rs` performs canonicalization/normalization and nearest-existing-component resolution.

The hard execution boundary is therefore stronger than the approval boundary, but the system has **two different path-resolution call paths**.

### Risk

An approval decision should be about the exact target that execution will use. Duplicated path semantics make it possible for policy and execution to disagree.

### Correct architecture

Use one shared resolver:

```text
request path
  -> normalize/resolve
  -> boundary classification
  -> approval decision
  -> execute the already-resolved target
```

Do not make policy independently reconstruct a path that execution later resolves again.

---

## C-03 — Generic resolution permits a denied action to be approved later

**Severity: HIGH**

`magy-core/src/application/approval.rs`, lines **137-149**:

```rust
if record.approval_status != ApprovalStatus::Pending
    && record.approval_status != ApprovalStatus::Denied
{
    return Err(Error::ActionNotPending);
}

if approved {
    record.approval_status = ApprovalStatus::Approved;
    ...
    execute_tool(agent, record.request.clone())
}
```

This creates a generic `Denied -> Approved` path.

That can be valid as an explicit override mechanism, but it should not be indistinguishable from normal pending approval.

### Required invariant

```text
Pending -> Approved
Pending -> Denied

Denied -> Approved ONLY through explicit override
```

A separate operation such as `override_denied_action` should make this semantic distinction explicit and auditable.

---

## C-04 — `/api/resolve` does not establish an explicit active-approval precondition

**Severity: HIGH**

The application endpoint passes an index and boolean decision to `resolve_pending_action()` without an explicit state check at the HTTP boundary.

That means correctness depends heavily on the trace record itself rather than on a complete endpoint-level authorization/state contract.

A robust implementation should reject stale decisions when:

- the run is no longer awaiting approval;
- the index is no longer the active pending action;
- another run has replaced the trace;
- the worker has resumed/terminated unexpectedly.

Approval is a state transition, not merely a mutable record field.

---

## C-05 — Shell command execution is full shell execution

**Severity: HIGH**

`magy-core/src/infrastructure/command.rs` executes on Windows via `cmd /C` and on Unix via `sh -c`.

That means an approved command is not a single executable plus fixed arguments. It is interpreted according to shell grammar, including command chaining, redirection, pipelines, expansion, and other shell behavior.

This is safe only if the approval boundary is real, visible, and explicit.

The allowlist is therefore best viewed as an **exact-command policy**, not as a general command parser.

---

## C-06 — Command output limit is applied after full capture

**Severity: HIGH**

`command.rs` defines a 1 MiB output limit, but `std::process::Command::output()` captures the entire stdout and stderr before the code truncates the buffers.

Therefore:

```text
MAX_COMMAND_OUTPUT_BYTES
```

limits retained/displayed output, not peak memory usage during capture.

A child process can still generate very large output and cause substantial memory growth.

### Correct fix

Read stdout/stderr incrementally and enforce the cap while streaming. Optionally terminate the child after the cap is exceeded.

---

## C-07 — Shell timeout does not establish full process-tree termination

**Severity: HIGH**

The timeout path kills the direct shell child, but shell-created descendants are not explicitly terminated as a process tree.

For a desktop engineering agent this matters because commands such as development servers can outlive the wrapper process.

Windows should use a Job Object/process-tree strategy or equivalent controlled process-group mechanism.

---

# 4. High-severity protocol and correctness findings

## H-01 — Planner does not check HTTP status before parsing JSON

`magy-core/src/infrastructure/model.rs` checks HTTP status in `ask_chat()`, but the Planner request path posts and immediately parses JSON.

A provider 500/429/etc. can therefore become a misleading JSON parse failure instead of a classified provider error.

**Recommendation:** check `response.status().is_success()` before deserializing.

---

## H-02 — Planner request serialization is incomplete

`planning.rs` builds a `PlannerRequest` containing goal, requirements, constraints, definition of done, existing tasks, and context.

`infrastructure/model.rs` only serializes the goal and requirements into the planner user message.

The source literally contains the note:

```text
// ... (Include other PlannerRequest fields if needed)
```

That means the application constructs planning context that the Planner provider does not actually receive.

This is a direct contract mismatch.

---

## H-03 — Planner output schema is less strict than Executor schema

The executor fallback schema explicitly sets:

```json
"additionalProperties": false
```

The Planner schema does not.

That weakens structural validation and creates inconsistent strictness between planner and executor protocols.

---

## H-04 — `MoveFile` exists in the domain model/parser but is absent from the Executor fallback schema

`domain/tool.rs` includes:

```text
MoveFile { from, to }
```

and the flat parser supports `move_file`.

The fallback Executor JSON schema in `infrastructure/model.rs` enumerates:

```text
read_file
write_file
list_directory
discover_files
git_status
git_diff
delete_file
run_command
task_complete
```

`move_file` is missing.

This is concrete tool/schema drift.

---

## H-05 — Executor prompt does not describe the full current tool set

The Executor system prompt documents `write_file`, `run_command`, and `task_complete`, but the current domain also exposes `delete_file` and `move_file`.

The model-facing documentation and actual capability surface therefore disagree.

---

## H-06 — Parser accepts arbitrary JSON embedded in surrounding text

`reasoning.rs` accepts direct JSON, fenced JSON, and then falls back to extracting a substring between the first `{` and the last `}`.

This is robust against chatty model output, but it weakens the guarantee that the model emitted exactly one structured tool object.

For a deterministic executor, strict structured output is preferable; the recovery path should be narrowly justified and heavily tested.

---

## H-07 — Project context has per-file limits but no total input budget

Filesystem reads cap individual files at 4 MiB and discovery at 100,000 entries.

`context_assembly.rs` can still assemble the entire project into the model context.

There is no total byte/token budget for the assembled request.

A large project can therefore exceed the provider's actual context window or create severe latency/memory pressure.

The runtime needs deterministic context budgeting and file prioritization.

---

# 5. Filesystem and boundary audit

## `magy-core/src/boundary.rs`

The boundary implementation is one of the stronger components.

It:

1. canonicalizes the selected root;
2. resolves relative paths against it;
3. normalizes path components;
4. walks toward the nearest existing component;
5. canonicalizes the nearest existing location;
6. checks containment relative to the canonical root;
7. uses symlink/reparse-aware filesystem metadata.

This is materially stronger than a simple `starts_with()` path-string check.

### Remaining concern

Validation and mutation occur as separate operations, so a local filesystem race can theoretically change a path after validation and before mutation. This is a TOCTOU class issue.

For a local desktop application the practical exposure is lower than a network service, but the invariant should still be documented.

---

## `magy-core/src/infrastructure/filesystem.rs`

### Strengths

- 4 MiB per-file limit.
- 10,000 directory-entry cap.
- 100,000 discovery-entry cap.
- atomic write strategy.
- explicit delete tool.
- separate move implementation.
- symlink/reparse handling during discovery.
- external-link tests.
- CWD-independence test.

### F-01 — Temporary filename collision

`write_file()` creates temporary names based on target filename plus process ID:

```text
.{filename}.magy-{pid}.tmp
```

Two concurrent writes to the same target from the same process can therefore contend for the same temporary name.

Use unique/exclusive temp creation semantics.

### F-02 — Delete is intentionally conservative

`delete_file()` deletes files only and rejects non-files.

This is good for the current safety model. Do not expand this into recursive directory deletion without a separately designed policy.

### F-03 — Move implementation and approval need one shared path-resolution primitive

The move tool separately validates source and destination. That should be expressed through the same canonical path resolver used by other file operations.

---

# 6. Command execution audit

## `magy-core/src/infrastructure/command.rs`

### Current behavior

- working directory is project root;
- shell command is executed through OS shell;
- timeout exists;
- exit code is preserved;
- stdout/stderr are returned;
- output is truncated after capture.

### Correctness/security concerns

1. Output cap is not a peak-memory cap.
2. Process descendants may survive timeout.
3. Command strings are shell programs, not argv arrays.
4. Exact allowlist matching must not be mistaken for shell sandboxing.

The safest model is to classify commands separately from execution and use argv-based execution whenever a command can be represented without shell syntax.

---

# 7. Approval policy audit

## Current policy

| Request | Current classification |
|---|---|
| ReadFile inside root | Approved |
| ReadFile outside root | Pending |
| ListDirectory inside root | Approved |
| ListDirectory outside root | Pending |
| DiscoverFiles | Approved |
| GitStatus | Approved |
| GitDiff | Approved |
| WriteFile inside root | Approved |
| WriteFile outside root | Pending |
| DeleteFile | Pending |
| MoveFile source+destination inside root | Approved |
| MoveFile crossing root | Pending |
| Exact allowlisted command + auto-approve | Approved |
| Exact allowlisted command + auto-approve off | Pending |
| Unknown non-empty command | Pending |
| Empty command | Denied |
| TaskComplete | Approved as a control signal |

This is substantially closer to the desired policy than a blanket approval model.

## Policy design recommendation

Use explicit classes:

```text
SAFE
  -> Approved

SENSITIVE
  -> Pending

FORBIDDEN
  -> Denied

Denied
  -> remains Denied unless explicit Override operation
```

The global `auto_approve_tools` setting should only affect classes explicitly designated as auto-approvable. It should not become a general escape hatch.

---

# 8. State machine audit

## `magy-core/src/domain/agent.rs`

This state machine represents coarse lifecycle state:

```text
Idle
Planning
Executing
Verifying
Paused(...)
Completed
Failed
```

It is internally coherent as an agent lifecycle.

## `magy-core/src/domain/model.rs`

This state machine is substantially finer-grained:

```text
Idle
Starting
AwaitingPlan
Planning
PlanValidation
ExecutingTask
AwaitingModel
AwaitingApproval
ExecutingTool
AwaitingVerification
Verifying
Recovering
Stalled
Completed
Failed
Cancelled
```

It is the better representation for a deterministic executor.

## Main architectural issue

The runtime must decide which state machine is authoritative.

Current code requires both, which creates the possibility of:

```text
Agent == Executing
RunState == AwaitingApproval
```

or another incompatible pair.

### Recommendation

Make `RunState` authoritative for execution and derive coarse `Agent` state from it, or strictly constrain `Agent` to long-lived project lifecycle while `RunState` owns transient execution lifecycle.

Do not allow both to independently encode the same transition.

---

# 9. Execution workflow audit

## `magy-core/src/application/coordinator.rs`

Coordinator owns:
- opening the project;
- starting trace;
- requesting plan;
- planning;
- validating/committing plan;
- selecting tasks;
- calling execution.

This is the correct high-level owner.

## `magy-core/src/application/execution.rs`

Execution owns:
- execution cycle;
- model reasoning step loop;
- repeated-action detection;
- approval handling;
- verification;
- completion checks;
- recovery/stall outcomes;
- history updates.

The main defect is that it also attempts to initiate planning from `Starting`.

### Repeated-action guard

The code records prior actions and stalls on repeated identical actions without new evidence. This is a good anti-loop invariant and should be preserved.

### Completion discipline

The code does not simply trust `TaskComplete` as proof. It checks unresolved actions and performs verification before marking the task completed.

That is one of Magy's strongest design decisions.

---

# 10. Model protocol audit

## Planner

### Strong

- separate provider role;
- structured response format;
- task ID checks;
- description checks;
- acceptance criterion checks;
- maximum task count;
- explicit project planning abstraction.

### Weak

- incomplete request serialization;
- HTTP status omission;
- incomplete schema strictness;
- dependency/DAG validation is not implemented despite dependency-aware planning language.

## Executor

### Strong

- one active task at a time;
- deterministic temperature of zero;
- typed output;
- explicit null-field contract;
- verification-aware history;
- no authority to modify the project plan directly.

### Weak

- schema/tool drift (`move_file` missing);
- prompt/tool drift (`delete_file`, `move_file` not fully documented);
- permissive JSON fallback parser;
- unbounded total context assembly.

---

# 11. Verification and evidence audit

The repository correctly treats model claims as untrusted.

The intended chain is:

```text
Model requests TaskComplete
        ↓
Runtime rejects unresolved actions
        ↓
Independent verification command
        ↓
Evidence recorded
        ↓
Verification result
        ↓
Task completion
```

This is architecturally sound.

The evidence model is stronger than simply asking a model to say whether its work succeeded.

---

# 12. Snapshot / recovery audit

The project has snapshot persistence and orphaned-run recovery mechanisms.

The key invariant to preserve is:

```text
Persisted worker state != proof that a worker process exists
```

A restart must reconstruct state conservatively. Recovery should never resume execution merely because a snapshot says the previous worker was active.

This area deserves integration tests that simulate process interruption and reload.

---

# 13. Application / API audit

## `magy-app/src/main.rs`

The desktop application combines:

- Axum local HTTP server;
- native Tao window;
- Wry webview;
- shared async application state;
- worker thread/task;
- SSE-style event stream.

Routes include project loading, initialization, run, cancel, events, approval resolution, GitHub information, chat, and settings.

### Positive

The worker is isolated from the UI thread and long-running model/command work uses blocking-task execution.

`auto_approve_tools` is re-read from shared state inside the worker loop, so a settings change can affect the next cycle rather than being frozen at the initial `/api/run` call.

### Risk

The application has several separate state surfaces:

- `AppState`
- `Agent`
- `ExecutionTrace`
- `RunState`
- `worker_active`
- persisted snapshot state
- frontend UI state

The more surfaces that describe “what is happening now,” the more important explicit state ownership becomes.

---

# 14. Frontend audit

## `magy-ui/index.html`

Defines the workbench shell, state/interaction regions, and approval surface.

The backend/frontend contract relies heavily on exact event and state names. These strings are effectively an API and should be centralized or schema-tested.

## `magy-ui/app.js`

The client is event-driven around project load, step events, run-state events, terminal outcomes, chat, settings, and approval UI.

Approval behavior is particularly sensitive to distinctions between:

```text
awaiting_approval
stalled
denied
completed
failed
```

The UI should not use `stalled` as a synonym for “approval pending.” A stalled run is an outcome; pending approval is a live authorization state.

## `magy-ui/style.css`

Presentation layer for the workbench, activity cards, chat/terminal areas, state indicators, and approval interaction zone.

The main engineering requirement here is keeping class/ID/state-name contracts synchronized with `app.js` and backend events.

---

# 15. CLI audit

## `magy-cli/src/main.rs`

The CLI is a separate front end over core functionality.

The important architectural invariant is that it should not create a second, different security policy. Core policy and filesystem enforcement should remain authoritative so CLI and desktop UI cannot diverge.

---

# 16. Documentation drift

The root README's current security-policy prose is stale relative to the implementation.

The README describes write operations as pending by default and command execution as denied by default, while the current code now classifies project-local `WriteFile` as approved and unknown commands as pending.

This is a concrete documentation/code mismatch.

Documentation should be regenerated from the actual policy matrix after the policy stabilizes.

---

# 17. Test coverage audit

The core contains meaningful unit tests for:

- path boundary behavior;
- external symlinks;
- broken links;
- Windows junction behavior;
- CWD independence;
- read/write/list/discovery;
- approval decisions;
- repeated-action behavior;
- provider failures;
- tool parsing;
- verification and completion behavior.

### Missing cross-layer tests

Add integration coverage for:

```text
Starting -> AwaitingPlan -> Planning -> BeginTask -> ExecutingTask
ExecutingTask -> AwaitingModel -> ExecutingTool
ExecutingTool -> AwaitingApproval -> Approved -> Execute
ExecutingTool -> AwaitingApproval -> Denied -> Stalled
Denied -> explicit Override -> Approved
stale approval after new run starts
concurrent approval resolution
move_file structured-output schema
command output > 1 MiB
command timeout with descendants
concurrent same-target writes
planner HTTP 500/429
oversized total project context
```

These are more valuable than simply adding more unit assertions around individual helper functions because the highest-risk defects are cross-layer state/policy interactions.

---

# 18. Recommended correction order

## Phase 1 — State authority

Remove planning initiation from `run_execution_cycle()` and let the coordinator own the planning transition.

Do not add UI behavior to compensate for an invalid state transition.

## Phase 2 — Approval state machine

Separate:

```text
Pending
Approved
Denied
Explicit Override
```

Make `/api/resolve` state-aware and reject stale/out-of-phase resolutions.

## Phase 3 — Unified path resolver

One resolver should produce the target and boundary classification used by both policy and execution.

## Phase 4 — Tool/schema parity

Make these five surfaces identical:

```text
domain ToolRequest
action parser
model JSON schema
Executor prompt
tool dispatcher
```

## Phase 5 — Command containment

Fix streaming output limits, process-tree cleanup, and command classification.

## Phase 6 — Context budget

Add a deterministic total context budget with explicit file-priority rules.

## Phase 7 — Integration gate

Run the complete workflow end-to-end under tests before another release build.

---

# 19. Final assessment

## Architecture

**Strong foundation.** The repository contains an actual runtime/security architecture rather than a thin model wrapper.

## Determinism

**Good.** Typed requests, explicit states, bounded steps, repeated-action detection, deterministic model temperature, and independent verification are all strong choices.

## Filesystem safety

**Strong.** Boundary handling, symlink awareness, write-size limits, and conservative delete semantics are materially good.

## Approval model

**Functional but not yet clean.** The current local-write policy is aligned with safe-by-default project development, but denied-action override and path-resolution ownership need refinement.

## State machine

**Primary architectural defect.** Two lifecycle models exist, and execution can incorrectly initiate a planning transition that the following code does not handle.

## Model protocol

**Usable but drifting.** Planner request serialization and tool-schema parity need correction.

## Command execution

**Needs hardening.** Shell execution, post-capture output limits, and incomplete process-tree termination are the largest operational concerns.

## Scaling

**Needs context controls.** Per-file limits are not enough without a total context budget.

## Tests

**Good unit foundation; incomplete integration gate.** The highest-value missing tests are cross-layer state/policy transitions.

## Release readiness

**Not yet demonstrated by this audit.** A clean release gate should require source review plus fresh `fmt`, full workspace tests, clippy, and release build execution in a working checkout.

---

# 20. Priority matrix

| ID | Finding | Severity | Primary file(s) |
|---|---|---|---|
| C-01 | Execution cycle can create unhandled `AwaitingPlan` and duplicates planning ownership | CRITICAL | `application/execution.rs`, `application/coordinator.rs`, `domain/model.rs` |
| C-02 | Approval and execution use separate path-resolution logic | HIGH | `application/approval.rs`, `boundary.rs`, `infrastructure/filesystem.rs` |
| C-03 | Generic resolution permits `Denied -> Approved` | HIGH | `application/approval.rs` |
| C-04 | Approval endpoint lacks explicit live-state/staleness contract | HIGH | `magy-app/src/main.rs` |
| C-05 | Approved commands are full shell programs | HIGH | `infrastructure/command.rs` |
| C-06 | Output cap occurs after full capture | HIGH | `infrastructure/command.rs` |
| C-07 | Timeout does not guarantee process-tree cleanup | HIGH | `infrastructure/command.rs` |
| H-01 | Planner does not check HTTP status | HIGH | `infrastructure/model.rs` |
| H-02 | Planner request fields are incompletely serialized | HIGH | `planning.rs`, `infrastructure/model.rs` |
| H-03 | Planner schema less strict | MEDIUM | `infrastructure/model.rs` |
| H-04 | `move_file` missing from Executor schema | HIGH | `domain/tool.rs`, `infrastructure/model.rs` |
| H-05 | Executor prompt omits current tools | MEDIUM | `infrastructure/model.rs` |
| H-06 | JSON parser accepts arbitrary embedded object | MEDIUM | `application/reasoning.rs` |
| H-07 | No total model-context budget | HIGH | `context_assembly.rs`, `infrastructure/model.rs` |
| F-01 | Same-process concurrent writes can collide on temp filename | MEDIUM | `infrastructure/filesystem.rs` |
| A-01 | Two overlapping lifecycle state models | HIGH | `domain/agent.rs`, `domain/model.rs` |
| D-01 | README security policy is stale | MEDIUM | `README.md` |

---

# 21. Bottom line

The codebase has a **legitimate systems architecture** and several strong safety invariants. The core issue is not that the security model is absent; it is that the model/runtime/policy/UI contracts are currently drifting across boundaries.

The first fix should be the state ownership defect in `execution.rs`. The second should be formalizing approval as a true state transition with explicit override semantics. The third should unify path resolution and eliminate tool/schema drift.

Do those before further UI work.

After those are stable, harden command execution and add end-to-end transition tests. That sequence will give you a much more trustworthy runtime than continuing to patch individual symptoms as they appear.

---

## Source references used during this audit

GitHub repository/tree and raw checked-in files on branch `magy-current-work`, including:

- `magy-core/src/application/approval.rs`
- `magy-core/src/application/context_assembly.rs`
- `magy-core/src/application/coordinator.rs`
- `magy-core/src/application/execution.rs`
- `magy-core/src/application/planning.rs`
- `magy-core/src/application/reasoning.rs`
- `magy-core/src/application/snapshot.rs`
- `magy-core/src/application/task_lifecycle.rs`
- `magy-core/src/application/tool_execution.rs`
- `magy-core/src/application/verification_runner.rs`
- `magy-core/src/domain/agent.rs`
- `magy-core/src/domain/model.rs`
- `magy-core/src/domain/project.rs`
- `magy-core/src/domain/tool.rs`
- `magy-core/src/infrastructure/command.rs`
- `magy-core/src/infrastructure/filesystem.rs`
- `magy-core/src/infrastructure/model.rs`
- `magy-core/src/boundary.rs`
- `magy-core/src/lib.rs`
- `magy-app/src/main.rs`
- `magy-cli/src/main.rs`
- `magy-ui/index.html`
- `magy-ui/app.js`
- `magy-ui/style.css`
- root `README.md`, `Cargo.toml`, scripts, logs, and captured request/response artifacts.
