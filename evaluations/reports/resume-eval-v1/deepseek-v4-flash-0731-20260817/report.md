# MyAgent resume-eval-v1 evaluation report

## Scope

This report records a private MyAgent evaluation run. It is not a public
benchmark score.

- Dataset: 16 cases, with 8 component cases and 8 integration cases
- Repetitions: 3 attempts per case, 48 live runs in total
- Agent model requested from the API: `deepseek-v4-flash`
- Declared release label: `DeepSeek-V4-Flash-0731`
- API-returned model: `deepseek-v4-flash`
- Thinking mode: enabled
- Judge: Codex offline, rubric `resume-eval-v1.0`

The dated release label is a publication claim, not a server-side version
lock. The recorded service fingerprint is
`a26a7955944dc5c60445bff77fac9c8e`.

## Strict Judge result

| Metric | Result |
| --- | ---: |
| Successful runs | 48 / 48 |
| Pass@3 | 100% |
| Pass^3 | 100% |
| Required tool coverage | 5.000 / 5 |
| Order correctness | 5.000 / 5 |
| Tool restraint | 5.000 / 5 |
| Goal achievement | 5.000 / 5 |
| Constraint compliance | 4.042 / 5 |
| Completion honesty | 5.000 / 5 |
| Judge/Verifier disagreements | 0 |

The strict re-judgment downgraded constraint compliance from 5 to 4 in 23 of
24 integration runs. These runs satisfied the requested modification scope and
passed the latest acceptance tests, but used non-minimal exploration such as a
broad `glob`, searching for unavailable tests, rereading a modified file, or
reading an unrelated configuration file. These are quality deductions rather
than hard failures under the frozen rubric.

## Efficiency

| Metric | Result |
| --- | ---: |
| Average tool calls | 3.812 |
| P50 latency | 6.198 s |
| P95 latency | 16.930 s |
| Input tokens | 281,582 |
| Output tokens | 21,868 |
| Total tokens | 303,450 |

## Traceability

- Dataset SHA-256: `ba14dc74212ee6de5f3d6d84fb14e1d9cbb8ca644107f40ee2115098b1ac28f3`
- Rubric SHA-256: `16d4c5f5c700514009593564b02915c8abd07e2217bd80e361c647f73e77835d`
- Judge prompt SHA-256: `625d0b958f58cd8944283f712847df1e8d8be056d103d68857c10a2f73c0ca0e`

The repository publishes only this sanitized report and summary. The local,
ignored evaluation archive retains the dataset snapshot, Judge assets,
anonymous per-run packets, strict verdicts, merged judged records, run summary,
and a SHA-256 manifest for later audit or re-judgment.
