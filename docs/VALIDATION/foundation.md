# I00 Foundation Acceptance Evidence

**Status:** Passed

**Accepted implementation commit:** `bd253097f3738ed5fd288c7c720dee55422c4c50`

**Observed:** 2026-08-23

## Local gate

Reference environment: Linux, CPython 3.11.15, uv 0.11.17.

| Check | Observed result |
|---|---|
| pytest | 22 passed |
| Branch coverage | 88.27%, threshold 80% |
| Ruff lint and format | Passed |
| strict mypy | Passed for 17 source files |
| Locked dependency resolution | 150 packages; CrewAI 1.15.17 is a mandatory dependency |
| Package build | Source distribution and universal wheel built successfully |
| Wheel resources | Local, cloud, and hybrid YAML presets present |
| Schema export | Three deterministic public JSON Schemas generated |
| Valid configuration CLI | Accepted and fingerprinted |
| Unsupported schema CLI | Refused with `ERR-VER-001`; automatic migration false |
| Secret canaries | Absent from configuration and error output |

## Remote matrix

[GitHub Actions run 32661947822](https://github.com/Y4NN777/mishkan/actions/runs/32661947822)
completed successfully for:

- Ubuntu: Python 3.11, 3.12, and 3.13;
- macOS: Python 3.11, 3.12, and 3.13.

Each job ran the locked sync, branch-coverage gate, Ruff, formatting verification, strict mypy,
schema drift check, and distribution build.

## Scope conclusion

I00 satisfies its contract-bearing foundation gate. It does not claim CrewAI execution,
repository discovery, planning, SQLite run persistence, or Ollama acceptance; those belong to I01.
