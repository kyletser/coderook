# CodeRook product continuation evidence — 2026-09-06

All runs requested `qwen3.8-flash` through OpenAI Chat at temperature 0. These are small diagnostic experiments, not leaderboard or production-readiness claims.

## Compaction followed by actual coding

Runtime candidate: `65ec75ba11e9d35e246f37722e5d13c55cde0928`. Four frozen synthetic 12–20-round histories, one continuation per task/policy; policy order counterbalanced. Original constraints occur early and are not repeated in the latest request. Both policies use the same tasks, model, tools, 12-step and 240-second continuation bounds. All baseline implementations pass the visible smoke but fail the external verifier; all independent reference implementations pass the verifier.

The production Compactor commits append-only projections and the production AgentRunner edits a temporary workspace. Verifier code is created in a separate directory only after the Agent returns. This assesses the final target function, preservation of the existing smoke file, and runtime termination—not exhaustive whole-workspace compliance or a real daemon/browser end-to-end flow. Full histories and repeated smoke logs are synthetic.

| Policy | Target-code verifier | Runtime + target verifier | Median input tokens | Total input | Total output | Median seconds |
|---|---:|---:|---:|---:|---:|---:|
| structured | 4/4 | 2/4 | 202,928 | 800,312 | 35,094 | 113.8 |
| adaptive_evidence | 4/4 | 3/4 | 152,621 | 671,352 | 42,704 | 132.2 |

Observed median input reduction: **24.8%**. Counts include summary generation and all continuation calls, including failed runs. Adaptive was slower by median wall time and produced more output tokens; do not claim a speedup or turn input-token reduction into a dollar-cost claim. With four pairs and one trial per pair, this is directional pilot evidence, not statistical superiority.

Both policies produced correct target code in all four tasks. Structured hit the step limit on json-record and duration; adaptive hit it on duration. Those runs remain failed for runtime completion. Every compaction applied, retained the ledger prefix and replayed to the same projected messages. The adaptive extractor pinned only one event in each fixture: do not call that deterministic coverage of every business constraint. Business constraints were checked separately by the code verifier.

Raw per-task results, individual usage events and control outcomes: [compaction-coding.json](compaction-coding.json). Original source report SHA256: `a9b7669ee72ec1c23ed1ae3cf52c065bb49fb04b5e3866d1dca89e662b6955cc`.

## Risk routing challenge

Sixteen single-author synthetic colloquial/negated requests were labelled before model execution and not used for tuning. The score is agreement with action-risk labels, not overall intent accuracy, actual permission bypasses, or coding quality.

| Method | Risk agreement | Underestimated | Overestimated | Classification usage events |
|---|---:|---:|---:|---:|
| rules_only | 7/16 | 5 | 4 | 0 |
| llm_only | 15/16 | 1 | 0 | 16 |
| hybrid | 10/16 | 2 | 4 | 6 |

Several incorrect rule predictions still had confidence 0.92, preventing hybrid semantic classification. This invalidates extrapolating the earlier clear-intent 56-prompt result to natural phrasing. Keep the old result with its scope, but do not advertise general 100% intent accuracy or zero risk misses. Permission enforcement is a separate tool-level contract; no attack execution was attempted in this diagnostic.

Pure-model median classification latency was about 7.1 seconds. Rule/hybrid medians at zero reflect timer granularity/zero-call branches, not literally free execution. This supports investigating contextual ambiguity handling in the main model turn, not adding a separate classifier to every request. Any router change needs a fresh challenge set rather than tuning and rescoring this one as held-out evidence.

All 48 rows: [router-challenges.json](router-challenges.json). Original report SHA256: `e3fc50079832469efdd01034ac19ebf0bc606f887346790d8e49b155c66d5d49`.

## Multi-agent replications and resulting fixes

Each candidate repeats the same independent multi-file task and quick explanation under single and routed policies. Different commits are defect replications, not a controlled performance comparison across revisions.

| Candidate | Policy / cohort | Contract passed | Tokens | Workers | Applies | Unreviewed writes |
|---|---|---:|---:|---:|---:|---:|
| af36e44 | single / independent_multi_file | 1/1 | 31,951 | 0 | 0 | 0 |
| af36e44 | single / quick | 1/1 | 12,854 | 0 | 0 | 0 |
| af36e44 | routed / independent_multi_file | 0/1 | 207,933 | 3 | 1 | 0 |
| af36e44 | routed / quick | 0/1 | 27,086 | 0 | 0 | 0 |
| 65ec75b | single / independent_multi_file | 1/1 | 36,121 | 0 | 0 | 0 |
| 65ec75b | single / quick | 0/1 | 24,399 | 0 | 0 | 0 |
| 65ec75b | routed / independent_multi_file | 0/1 | 129,471 | 3 | 0 | 0 |
| 65ec75b | routed / quick | 0/1 | 29,103 | 0 | 0 | 0 |
| a7c9ef6 | single / independent_multi_file | 1/1 | 36,048 | 0 | 0 | 0 |
| a7c9ef6 | single / quick | 0/1 | 25,009 | 0 | 0 | 0 |
| a7c9ef6 | routed / independent_multi_file | 0/1 | 93,630 | 3 | 2 | 0 |
| a7c9ef6 | routed / quick | 0/1 | 24,814 | 0 | 0 | 0 |

Fix `65ec75b`: explicit Python compilation ignores PYTHONDONTWRITEBYTECODE. Worker commands now redirect Python caches to temporary execution storage. Fix `a7c9ef6`: an untracked content-addressed output Artifact must not make the main workspace ineligible for reviewed apply. Tracked Artifacts, configuration and other user changes remain visible. Related checks: 24 passed / 2 skipped for the first fix, 13 passed for the second, plus focused Ruff/Mypy; no full suite or push was performed.

The final routed multi-file run applied two verified Workers with zero apply conflicts and zero unreviewed main-workspace writes. The slugify Worker exhausted its own budget; its missing change caused overall task failure. Quick-task answers passed their code/fact verifier in the latter replications but exceeded the unchanged 20,000-token task contract. A resource-contract failure is not the same as an incorrect answer. Root three-Worker limits held in all three replications. This supports explicit bounded delegation and review semantics, not a multi-agent speed or quality advantage.

All 12 exported rows: [multiagent-replications.json](multiagent-replications.json). Original traces/receipts remain locally retained; this export omits private paths and proxy dollar fields, and includes original report hashes.

## Reproduce

```powershell
uv run python scripts/run_compaction_coding_experiment.py --dataset benchmarks/reliability/compaction_coding_tasks.json --output .benchmark-results/new-coding-run --preflight
uv run python scripts/run_compaction_coding_experiment.py --dataset benchmarks/reliability/compaction_coding_tasks.json --output .benchmark-results/new-coding-run
uv run python scripts/run_router_risk_challenges.py --dataset benchmarks/reliability/router_risk_challenges.json --output .benchmark-results/new-router-run
uv run python scripts/run_multiagent_strategy_experiment.py --expected-model qwen3.8-flash --allow-unknown-pricing --multi-limit 1 --quick-limit 1 --policy single --policy routed --output .benchmark-results/new-multiagent-run
```

Use a clean candidate and the same verified model route. The coding and challenge scripts/datasets are frozen by SHA256 in their reports, independently of their later publication location. No actual dollar cost is asserted because the local price registry lacks this model. Task/step limits are disclosed experiment contracts; the user explicitly removed the earlier dollar-denominated experiment stop.

Publication-only correction: the coding script now saves the raw Ledger byte-for-byte and
redacts only the derived event export. The executed script tried a plain path replacement
on the copied JSONL, which would break checksums for unescaped POSIX paths. On this Windows
run the JSON-escaped paths did not match that replacement; all eight retained copies were
checked to have zero checksum errors. This output-copy correction does not change the
executed model/context/verifier logic or the numbers above, but the published coding-script
hash differs from the executed hash retained in the report. Do not publish raw session logs
without a separate privacy review.

Earlier evidence (12-context recall, 25 crash injections, three external Python tasks): [previous candidate report](../2026-09-reliability-candidate/README.md). Those results keep their original commits and were not rerun by this publication.
