# Working on olympdev

Tooling for competitive-programming problems: an isolate sandbox, a
content-addressed store, and pipelines that test solutions.

## The split that decides most design questions

There are two sides, and code belongs to exactly one:

| Side | Has | Modules |
|---|---|---|
| **Submitter** | the problem directory, source files, host paths | `problem/`, `frontend/` |
| **Worker** | digests only — never a problem directory | `worker/`, `isolate/` |

`problem/build.py` is the single crossing point: it reads files through a
`ProblemPackage`, uploads them, and hands the worker digests. A worker must be
able to run everything it is given on a machine that has never seen the
problem's files.

When unsure where something goes, ask which side owns it. A `Path` on the
worker side is almost always a mistake.

**Layer order** (each may import those above it, never the reverse):
`settings` → `schema`, `storage` → `isolate` → `worker` → `problem` → `frontend`.
`settings.py` imports nothing from `olympdev` at all, so anything may depend on
it. `worker/pipelines/*` may import `problem.types` (plain models); nothing in
`worker/` may import `problem.package` or `problem.build`.

## Recipes

### Adding a pipeline

1. **Accessors on `ProblemPackage`** for whatever files the pipeline needs —
   the way [`role_files()`](olympdev/problem/package.py) works. Disk knowledge
   only: no storage, no digests, no pipeline types.
2. **`build_<x>_input(storage, package, ...) -> str` in `problem/build.py`.**
   This owns *every* disk touch: resolving paths, uploading blobs, picking
   toolchains. Reuse [`compile_spec`](olympdev/problem/build.py) for anything
   that compiles.
3. **The pipeline itself** in `worker/pipelines/`, subclassing
   `AbstractPipeline`:
   - set a stable `pipeline_type` ClassVar (it keys the restore registry);
   - `make_input_digest` should take *already-resolved data* — digests and
     plain values. `ProblemTestingPipeline.make_input_digest` still reads test
     files off disk via `problem_dir`; that is the one thing in it not to copy;
   - keep **all** decision state in the step table (`issue()`, `step()`,
     `has_step()`, `step_in_flight()`, `step_result()`) and child registry
     (`add_child()`, `children()`). Anything else you store must be a cache
     re-derivable from storage, or `save_state()`/`restore()` silently loses
     work;
   - return `WAITING` when progress depends on jobs already issued; call
     `complete()` or `fail()` before returning `FINISHED`;
   - never open a sandbox or call `job.execute()` — pipelines only hand out
     jobs.
4. **A state round-trip test** (see `tests/test_pipeline_state.py`): drive it
   partway, `save_state()` → storage → `restore()`, finish, and assert no job
   was issued twice. This is what actually catches hidden state.

**Open-ended child count** (see `worker/pipelines/stress.py`): a pipeline
whose number of children isn't known at `make_input_digest` time — because it
depends on when, or whether, something happens at runtime — should derive
"how many issued so far" from `len(self._children)` alone, add at most one
new child per `_next_job` call, and address a child by its exact computed key
rather than list position. Name numeric children zero-padded
(`iter-00000007`, not `iter-7`): `PipelineState` round-trips through storage
as `sort_keys=True` JSON (`olympdev/storage/abstract.py`), so an unpadded
`"iter-9"` sorts after `"iter-10"` and silently reorders the child dict on
restore once there are 10 or more.

### Adding a program role

Add one entry to `ROLE_SPECS` in [`worker/roles.py`](olympdev/worker/roles.py)
and a `CachePolicy` member. Job classes, labels, compile-failure verdicts and
pipeline loops all follow from the table — do not hand-write job subclasses.

### Adding a job

- Unique, stable `job_type`; subclass the right base so it inherits a sandbox
  profile (`SandboxRunJob.sandbox_profile`).
- `make_input_digest` uploads through `_upload_input`.
- `_parse_arguments` re-validates **everything** on the worker: every digest
  through `storage.validate_digest`, every sandbox path through
  `sandbox_relpath`. Stored input is untrusted — it may have been produced
  elsewhere.
- Give it a cache policy; `CachePolicy.Never` for anything not safely reusable.

### Adding a frontend view

- View model in [`frontend/view.py`](olympdev/frontend/view.py): plain data,
  no HTML, no colours, and **no jinja2 import** — a worker installs olympdev
  without the `notebook` extra and must still print reports. Domain decisions
  (which clock to show, how memory is derived) live here, not in a template.
- Then a Jinja template taking that one view model, with styling in
  `_base.css`, and a matching function in `frontend/text.py`.
- To watch a run, implement `PipelineObserver` — **never copy a driver loop.**
  The old notebook did, and its copy silently stopped going through
  `JobExecutor`, so caching quietly did nothing.

## Hard rules

- **Never widen the sandbox from a call site.** `IsolateSandBox.run()` takes a
  fixed profile; processes, environment and mounts are decided in
  [`isolate/profiles.py`](olympdev/isolate/profiles.py). Do not re-introduce
  `full_env`, per-call `dir_rules`, or a flag that skips a memory limit.
- **Never derive a module-level constant from settings.** Caps are checked when
  a value is built; a module-level `IsolateLimits` made a tight host config
  raise on *import*, breaking even `olympdev config`. Use a function (see
  `compile_job_limits()`).
- **`job_type`, `pipeline_type` and stored dict keys are on-disk format.**
  Changing one invalidates stored inputs and breaks restore; never reuse an old
  name for a new meaning.
- **`info.json` field names are the model's field names.** No translation
  layer: if a field is awkward, rename it in both places.
- **Treat everything from a problem or a submission as untrusted.** A
  participant controls only source bytes; a problem author controls limits
  (within caps), compile flags (within the allow/denylist), generator argv and
  test files under the problem directory. Everything else is the server's.
- **Storage blobs are immutable and content-addressed.** Writes go through a
  temp file and `os.replace`; never assume the on-disk layout (blobs are
  sharded).

## Tests

- `pytest tests -m "not slow"` for the fast loop; `pytest tests` runs isolate
  and g++ too. Mark anything that compiles or sandboxes as `slow`.
- `tests/attacks/` runs adversarial payloads through the real pipeline. Its
  `conftest.py` marks only its own directory — a hook there receives the whole
  session's items, and marking everything once made `-m "not slow"` empty.
- Tests pin settings through the autouse `default_settings` fixture; override
  with `using_settings(...)` rather than mutating globals.
- Prefer asserting behaviour over shape. Storage tests must not assume a flat
  blob layout; pipeline tests should assert verdicts and issued jobs.

## Commands

```bash
pip install -e ".[notebook]"      # core install omits jinja2 on purpose
olympdev verify example-contest   # end-to-end check on the sample problem
olympdev config                   # which settings applied, and from which file
pytest tests -m "not slow"
```

Settings come from `$OLYMPDEV_CONFIG` → `./olympdev.toml` →
`~/.config/olympdev/config.toml` → `/etc/olympdev/config.toml` → defaults.
`olympdev.example.toml` documents every key.
