# Context-reduction verification (reaudit-sample)

Timestamp: 2026-08-11T01:19:17+00:00
Passed: **False**


| item | persisted (pre-change) | flags-on | flags-off | input_tokens | status | divergence |
|---|---|---|---|---|---|---|
| SA-0MS0BP707003KGRM | No | Yes | Yes | [2590, 2316] | PASS |  |
| SA-0MS3CITYB006E3DT | No | No | - | [1863, 757] | DIVERGE | audit timed out (no verdict) |
| SA-0MS0QF0ZC0009CI5 | No | Yes | No | [2476, 2698] | DIVERGE | verdict differs with/without flags: on=Yes off=No |
| SA-0MS8WAJAQ004VWTX | Yes | - | - | [] | DIVERGE | audit timed out (no verdict) |
| SA-0MS1WXHVF008AKQW | Yes | No | No | [1095, 1127] | PASS |  |

Per-call input tokens (AC2):

- SA-0MS0BP707003KGRM: 2590
- SA-0MS0BP707003KGRM: 2316
- SA-0MS3CITYB006E3DT: 1863
- SA-0MS3CITYB006E3DT: 757
- SA-0MS0QF0ZC0009CI5: 2476
- SA-0MS0QF0ZC0009CI5: 2698
- SA-0MS1WXHVF008AKQW: 1095
- SA-0MS1WXHVF008AKQW: 1127
