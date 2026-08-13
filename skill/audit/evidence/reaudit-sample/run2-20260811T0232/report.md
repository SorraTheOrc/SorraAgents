# Context-reduction verification (reaudit-sample)

Timestamp: 2026-08-11T02:59:39+00:00
Passed: **False**


| item | persisted (pre-change) | flags-on | flags-off | input_tokens | status | divergence |
|---|---|---|---|---|---|---|
| SA-0MS3CITYB006E3DT | No | Yes | Yes | [71, 722] | PASS |  |
| SA-0MS0QF0ZC0009CI5 | No | No | No | [673, 829] | PASS |  |
| SA-0MS8WAJAQ004VWTX | Yes | No | Yes | [171, 2482] | DIVERGE | verdict differs with/without flags: on=No off=Yes |

Per-call input tokens (AC2):

- SA-0MS3CITYB006E3DT: 71
- SA-0MS3CITYB006E3DT: 722
- SA-0MS0QF0ZC0009CI5: 673
- SA-0MS0QF0ZC0009CI5: 829
- SA-0MS8WAJAQ004VWTX: 171
- SA-0MS8WAJAQ004VWTX: 2482
