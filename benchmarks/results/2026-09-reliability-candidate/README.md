# CodeRook reliability candidate evidence

This report keeps the successful and unsuccessful measurements produced while validating the
reliability-oriented agent design. It does not claim production readiness or leaderboard parity.
Every number below comes from a retained JSON report, and no best-of-N result was selected.

## Candidate results

| Experiment | Commit | Result | What it supports |
|---|---|---:|---|
| Aider Polyglot fixed Python slice | `6aa638b` | 3/3 | The real Agent loop can edit and verify external tasks under WSL2+bwrap |
| Evidence-preserving compaction | `6aa638b` | 12/12 | The adaptive projection retained all declared facts while reducing repeated context |
| Five-phase crash recovery | `6aa638b` | 25/25 | Durable recovery did not duplicate modifications or leave high-risk child processes |
| Task router labelled ablation | `1d5e936` | 56 prompts | Deterministic signals handled the frozen clear-intent corpus without an extra model call |
| Routed multi-agent pilot | `1d5e936` | 2/6 cells passed | Multi-agent execution is model-dependent and must remain bounded rather than default-on |

The detailed machine-readable aggregate is in [`evidence.json`](evidence.json).

## External coding tasks

The public slice is selected from `Aider-AI/polyglot-benchmark` commit
`7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f`. For Python, CodeRook sorts eligible baseline-failing
instance IDs by `SHA256(instance_id + "coderook-v1")` and takes the first three; the tasks were not
chosen by looking at their result.

Using `qwen3.8-flash`, temperature 0, at most 20 agent steps and 600 seconds per task:

| Task | Official verifier | First edit correct | Tokens | Time |
|---|---:|---:|---:|---:|
| `python-grade-school` | pass | yes | 42,179 | 18.0 s |
| `python-wordy` | pass | yes | 59,497 | 110.8 s |
| `python-scale-generator` | pass | yes | 49,261 | 37.3 s |

Aggregate: 3/3 passed, 150,937 total tokens, 37.3 s P50, 110.8 s P95, and zero non-target file
changes. This is a three-task reproducibility slice, not a claim about the full Polyglot benchmark.

A previous GLM run on the same fixed task IDs also passed 3/3, but it used commit `5c7ce70`; it is
reported only as cross-provider corroboration, not as a strict model benchmark. Qwen used 15.5% fewer
tokens and had a lower observed P95 in these runs, but the sample is too small for a general model
ranking.

## Long-context compaction

The compaction set contains 12 synthetic, frozen 12-20-turn conversations. It measures whether the
model can recover declared task facts after three context policies; it is not coding pass@1.

| Policy | Probe pass | Fact retention | Median model input | Duplicate reads removed | Fallbacks |
|---|---:|---:|---:|---:|---:|
| Head/tail truncate | 0/12 | 25% | 2,733 | 0 | 0 |
| Structured summary | 11/12 | 100% | 13,144 | 0 | 1 |
| Adaptive evidence | 12/12 | 100% | 7,160 | 114 | 2 |

Adaptive evidence reduced median model input by 45.5% versus the structured baseline while improving
the probe result by one task. Its quality gate rejected two summaries that omitted required error
evidence and fell back to the original context. Those fallbacks are counted, because saving tokens is
not allowed to override correctness.

## Routing and multi-agent boundary

On the single-author frozen set of 56 clear-intent Chinese and English prompts:

| Router | Intent Macro-F1 | Exact profile | Risk false negatives | Model calls | Median latency |
|---|---:|---:|---:|---:|---:|
| Rules only | 1.000 | 100% | 0 | 0 | 0.082 ms |
| Qwen only | 0.964 | 71.4% | 0 | 56 | 6,471 ms |
| Hybrid | 1.000 | 100% | 0 | 0 | 0.139 ms |

The corpus intentionally contains clear signals, so this supports the decision to avoid an extra LLM
classification call for ordinary requests. It does not establish routing quality on ambiguous real
user traffic.

The Qwen multi-agent pilot produced an important negative result. Single Agent passed the independent
multi-file task in 53.2 seconds and 28,789 tokens. Routed delegation exhausted its shared budget,
started seven Workers across repeated plans, and failed after 280.1 seconds and 397,472 tokens. All
unreviewed main-workspace write counts remained zero. CodeRook was then changed to enforce a hard
three-Worker limit across the entire root Turn, including replanning. Multi-agent remains an explicit,
bounded strategy rather than a default performance claim.

## Crash recovery

The deterministic local matrix hard-killed and restarted the real daemon five times in each of five
failure windows: model request in flight, tool call persisted before execution, managed Shell process,
permission wait, and tool result persisted before Turn finish.

All 25 runs recovered. The report recorded zero duplicate modifications, orphan tool calls, Ledger
errors, and orphan high-risk processes. Recovery latency was 5.233 s P50 and 5.718 s P95 on Windows.
This validates recovery semantics only; it says nothing about model coding quality.

## Reproduction

Use a clean checkout and a Provider that has passed `coderook provider test`:

```bash
uv run python scripts/run_strategy_router_experiment.py \
  --method rules_only --method llm_only --method hybrid \
  --task-limit 56 --allow-model-calls --allow-unknown-pricing

uv run python scripts/run_compaction_experiment.py \
  --task-limit 12 --repeats 1 --allow-unknown-pricing

uv run python scripts/run_multiagent_strategy_experiment.py \
  --multi-limit 1 --quick-limit 1 \
  --policy single --policy always_delegate --policy routed \
  --allow-unknown-pricing

uv run python scripts/run_crash_recovery_matrix.py --iterations 25
```

The public benchmark additionally requires the pinned dataset checkout and either a disposable
container or a working WSL2+bwrap probe. API keys and local absolute paths are not stored here.
