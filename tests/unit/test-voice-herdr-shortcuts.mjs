/**
 * Unit tests for pi-client/voice-input/herdr-shortcuts.js
 *
 * Acceptance criteria covered (SA-0MUX9Y4LX000KF3P):
 *   - spoken phrases map to Herdr chords and are injected into the target pane
 *   - phrase matching is case/whitespace/punctuation insensitive
 *   - an explicit pane id wins; otherwise the pane label is resolved in the
 *     calling workspace
 *   - failures notify and never throw
 *
 * Herdr is never invoked: the runner's `run` dependency is injected.
 *
 * Run: node --test tests/unit/test-voice-herdr-shortcuts.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "voice-input", "herdr-shortcuts.js");

const {
  normalisePhrase,
  matchShortcut,
  resolveTargetPane,
  createHerdrShortcutRunner,
} = await import(MODULE);

const SHORTCUTS = [
  { phrase: "producer interview", chord: ["r", "i"] },
  { phrase: "Producer Review", chord: ["r", "p"] },
];

// ---------------------------------------------------------------------------
// Phrase matching
// ---------------------------------------------------------------------------

describe("herdr shortcut phrase matching", () => {
  test("normalises case, whitespace and trailing punctuation", () => {
    assert.equal(normalisePhrase("  Producer   INTERVIEW! "), "producer interview");
    assert.equal(normalisePhrase("r i."), "r i");
    assert.equal(normalisePhrase(undefined), "");
  });

  test("matches a configured phrase regardless of case/whitespace/punctuation", () => {
    assert.deepEqual(matchShortcut(SHORTCUTS, "Producer Interview"), SHORTCUTS[0]);
    assert.deepEqual(matchShortcut(SHORTCUTS, "  producer   interview.  "), SHORTCUTS[0]);
    assert.deepEqual(matchShortcut(SHORTCUTS, "producer review"), SHORTCUTS[1]);
  });

  test("returns null for no match, empty transcript or no shortcuts", () => {
    assert.equal(matchShortcut(SHORTCUTS, "open the worklist"), null);
    assert.equal(matchShortcut(SHORTCUTS, ""), null);
    assert.equal(matchShortcut(SHORTCUTS, "producer"), null);
    assert.equal(matchShortcut([], "producer interview"), null);
  });
});

// ---------------------------------------------------------------------------
// Target pane resolution
// ---------------------------------------------------------------------------

describe("herdr target pane resolution", () => {
  const PANES = [
    { pane_id: "w3E:p3V", label: "Work Items" },
    { pane_id: "w3E:p3Z", terminal_title_stripped: "pi - SorraAgents" },
  ];

  test("an explicit pane id wins", () => {
    assert.equal(resolveTargetPane(PANES, { targetPaneId: "w9:p1" }), "w9:p1");
  });

  test("resolves by label or stripped terminal title (case-insensitive)", () => {
    assert.equal(resolveTargetPane(PANES, { targetPaneLabel: "work items" }), "w3E:p3V");
    assert.equal(
      resolveTargetPane(PANES, { targetPaneLabel: "PI - SorraAgents" }),
      "w3E:p3Z",
    );
  });

  test("returns null when no pane matches", () => {
    assert.equal(resolveTargetPane(PANES, { targetPaneLabel: "Nope" }), null);
    assert.equal(resolveTargetPane([], { targetPaneLabel: "Work Items" }), null);
  });
});

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

/** Fake herdr runner that records calls and replays pane list payloads. */
function harness({ panes = [], runError = null } = {}) {
  const calls = [];
  const notifications = [];
  const run = (args) => {
    calls.push(args);
    if (runError) throw new Error(runError);
    if (args[0] === "pane" && args[1] === "list") return { result: { panes } };
    return { result: {} };
  };
  const sendShortcut = createHerdrShortcutRunner({
    shortcuts: SHORTCUTS,
    targetPaneLabel: "Work Items",
    workspaceId: "w3E",
    run,
    notify: (message, level) => notifications.push({ message, level }),
  });
  return { sendShortcut, calls, notifications };
}

describe("herdr voice shortcut runner", () => {
  test("sends the chord to the labelled pane in the calling workspace", async () => {
    const { sendShortcut, calls, notifications } = harness({
      panes: [{ pane_id: "w3E:p3V", label: "Work Items" }],
    });

    const handled = await sendShortcut("Producer Interview");

    assert.equal(handled, true);
    assert.deepEqual(calls[0], ["pane", "list", "--workspace", "w3E"]);
    assert.deepEqual(calls[1], ["pane", "send-keys", "w3E:p3V", "r", "i"]);
    assert.deepEqual(notifications, []);
  });

  test("an unmatched transcript is not handled and runs no herdr command", async () => {
    const { sendShortcut, calls } = harness({ panes: [] });
    assert.equal(await sendShortcut("open the worklist"), false);
    assert.deepEqual(calls, []);
  });

  test("an explicit target pane id skips pane discovery", async () => {
    const calls = [];
    const sendShortcut = createHerdrShortcutRunner({
      shortcuts: SHORTCUTS,
      targetPaneId: "w1:p9",
      run: (args) => {
        calls.push(args);
        return { result: {} };
      },
    });

    assert.equal(await sendShortcut("producer review"), true);
    assert.deepEqual(calls, [["pane", "send-keys", "w1:p9", "r", "p"]]);
  });

  test("notifies when no pane matches and does not send keys", async () => {
    const { sendShortcut, calls, notifications } = harness({ panes: [] });

    const handled = await sendShortcut("producer interview");

    assert.equal(handled, true);
    assert.equal(calls.length, 1); // pane list only
    assert.equal(notifications.length, 1);
    assert.match(notifications[0].message, /no Herdr pane labelled "Work Items"/);
    assert.equal(notifications[0].level, "warning");
  });

  test("a herdr failure notifies and never throws", async () => {
    const { sendShortcut, notifications } = harness({ runError: "herdr not found" });

    const handled = await sendShortcut("producer interview");

    assert.equal(handled, true);
    assert.equal(notifications.length, 1);
    assert.match(notifications[0].message, /herdr not found/);
  });

  test("a shortcut with an empty chord is handled without running herdr", async () => {
    const calls = [];
    const sendShortcut = createHerdrShortcutRunner({
      shortcuts: [{ phrase: "noop", chord: [] }],
      run: (args) => {
        calls.push(args);
        return {};
      },
    });
    assert.equal(await sendShortcut("noop"), true);
    assert.deepEqual(calls, []);
  });
});
