# Tool-output budget guidance

Canonical guidance on sizing every `read`, `rg` (ripgrep), `cat`, `jq` and
`wl` call so that an implementation session preserves context to the end
rather than burning it on raw output in the first ten minutes.

**Single source of truth** — this page is the one reference every skill must
consult. Delivered by SA-0MWJYHKDX006HS36. Evidence base from session
`AH-0MWH4F375000X08G` (see parent SA-0MWJWJ1HV000EBKP).

## The pattern

**size before you read** — never issue a call that could return more than
~10 KB without first establishing how large the output will be. State the
expected size before making the call.

1. **Measure or project the output** — `jq length`, `rg -c`, `wc -l`, `jq
   'keys'` — learn the shape before fetching the body.
2. **Narrow the fetch** — `read` with `offset`/`limit`, `rg -m 5`,
   `sed -n 'A,Bp'` — pull only what you need.
3. **Do not re-fetch what you already hold** — one `wl show` per work item
   per session (it is already in context after the claim step); never re-read
   a skill doc or reference page you have already read this session.
4. **Justify anything large** — a single tool result over ~10 KB must have a
   clear reason; an unexplained 50 KB result is a defect in the call, not in
   the tool.

### Worked examples

**1. Reading a JSON baseline — never `cat` a generated artefact**

```bash
# WASTEFUL — dumps the entire 50 KB file; you may never need most of it
cat docs/dev/ring-timing-baseline.json
```

```bash
# SIZE-PROVEN — project first, then read what you need
jq 'keys' docs/dev/ring-timing-baseline.json   # learns the shape
jq '.rings[0]' docs/dev/ring-timing-baseline.json  # one ring, ~2 KB
jq -c '.[] | {ringIndex, strategy}' docs/dev/ring-timing-baseline.json
```

**2. Searching a large document — cap exploratory `rg`**

Never use `-A 20` on a first pass over a large document; it multiplies
context on every hit. Count or list first, then widen only once the result
set is proven small.

```bash
# WASTEFUL — first-pass -A 20 pulls context on every hit across a 400-line doc
rg -A 20 "8.2\|Hex variety\|personality\|contract\|landmark" game.gd
```

```bash
# SIZE-PROVEN — learn the count first, then narrow
rg -c "8.2\|Hex variety" game.gd                 # 3 matching lines
rg -n -A 5 "8.2\|Hex variety" game.gd           # only 3 hits — safe context
```

**3. Reading code around a symbol — locate, then read a slice**

```bash
# WASTEFUL — reads 29 KB of hexRing.ts to find three field names
read src/hexRing.ts
```

```bash
# SIZE-PROVEN — locate the definition, read the surrounding code
rg -n "class HexRing" src/hexRing.ts            # line 42
read src/hexRing.ts --offset 42 --limit 30      # ~3 KB, exactly the class
```

**4. Re-fetching context already held — don't pay twice**

```bash
# WASTEFUL — the claim step already fetched this work item into context
wl show SA-0XXXX --json                         # ~20 KB, already held
rg -n "class HexRing" skill/implement/SKILL.md  # re-reading a doc read earlier
```

```bash
# SIZE-PROVEN — reuse what you already have
# The work item is already in context after the claim — do not `wl show` it again.
# A skill doc already read this session stays in context — do not re-read it.
```

## Applicability

Any skill step that issues `read`, `bash` (including `rg`, `cat`, `jq`, `wl
show`), or equivalent file-reading or search calls should follow this pattern.
Steps that are already synchronous/foreground (a blocking subprocess call
with a timeout) need no change.

## Measured evidence (parent session)

Analysis of a live `implement` session (SA-0MWJWJ1HV000EBKP) in `AI_Hell`
(`AH-0MWH4F375000X08G`), 2026-11-16:

| Component | Bytes | Share of context |
|---|---|---|
| System prompt (static startup) | 51 KB | 10 % |
| **toolResult messages (63 calls)** | **409 KB** | **83 %** |
| Assistant text + thinking (63 turns) | 34 KB | 7 % |

Eight tool calls produced 64 % of all tool output (and 53 % of the whole
context) in 8 min 34 s — the dynamic runtime surface, where no budget
guidance existed before this doc.
