# MyAgent resume-eval-v2 evaluation report

## Scope

This report records a private MyAgent evaluation run. It is not a public
benchmark score.

- Dataset: 50 cases, with 25 component cases and 25 integration cases
- Repetitions: 3 attempts per case, 150 live runs in total
- Agent model requested from the API: `deepseek-v4-flash`
- Declared release label: `DeepSeek-V4-Flash-0731`
- API-returned model: `deepseek-v4-flash`
- Thinking mode: enabled
- Judge: Codex offline, rubric `resume-eval-v1.0`

The dated release label is a publication claim, not a server-side version
lock. The recorded service fingerprint is
`a26a7955944dc5c60445bff77fac9c8e`.

## Audited run result

| Metric | Result |
| --- | ---: |
| Hard-verifier successful runs | 150 / 150 |
| Strict-Judge successful runs | 146 / 150 (97.33%) |
| Judge Pass@3 | 100% |
| Judge Pass^3 | 94% |
| Required tool coverage | 4.947 / 5 |
| Order correctness | 4.922 / 5 |
| Tool restraint | 4.893 / 5 |
| Goal achievement | 5.000 / 5 |
| Constraint compliance | 4.080 / 5 |
| Completion honesty | 4.960 / 5 |
| Judge/Verifier disagreements | 0 |

The first 150-run execution produced 149 verifier passes. The single failure,
`stable_unique_names#1`, was an evaluator-invalid sample: randomized Python set
iteration intermittently made the intentionally faulty baseline match the
expected order. The grader subprocess was fixed with `PYTHONHASHSEED=0`, the
baseline was repeated ten times successfully, and only the matching case and
attempt were rerun. The original invalid record and replacement record remain
in the ignored local audit archive.

The strict Judge rejected four otherwise functionally correct component runs.
`component_grep_edit_status#1` and `#2` used a forbidden `glob` and exceeded the
call limit. `component_two_file_read_one_edit#2` and
`component_fix_exact_line#2` used forbidden `write_file` in place of required
`edit_file`. All 75 integration runs passed the strict Judge.

## Efficiency

| Metric | Result |
| --- | ---: |
| Average tool calls | 3.987 |
| P50 latency | 5.057 s |
| P95 latency | 16.589 s |
| Input tokens | 1,000,871 |
| Output tokens | 71,390 |
| Total tokens | 1,072,261 |

## Traceability

- Dataset SHA-256: `02508c7240c36e8be77757b69d5d3fa30a12bd0e17534bb931be65310161892c`
- Rubric SHA-256: `7d8d28f14953274347e1f5c08bf64b336aa1589efff4217ae966b97ae52fc886`
- Judge prompt SHA-256: `95a66c92b379fec13af750e6b790dcf847bdb09eea79dc08ff56d2bc553f995b`

The repository publishes only this sanitized report and summary. The local,
ignored evaluation archive retains raw runs, anonymous Judge packets, verdicts,
merged judged records, the invalidated sample, its replacement, and the repair
manifest for later audit or re-judgment.
