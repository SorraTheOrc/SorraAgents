/**
 * voice-input / herdr-shortcuts — drive a Herdr pane's keybindings by voice.
 *
 * An operator can map a spoken phrase to a Herdr chord (e.g. "producer
 * interview" → `r` `i`). When the final transcript matches a configured
 * phrase, the extension injects the chord into the target pane with
 * `herdr pane send-keys` so the plugin's own shortcut fires. This is an
 * explicit allowlist — arbitrary shell commands are never run from speech.
 *
 * The logic is dependency-free (`.js`) with an injectable `run` function so it
 * is unit testable without a live Herdr session. `index.ts` wires the real
 * `runHerdr` (spawnSync) implementation.
 */

import { spawnSync } from "node:child_process";

/** Default Herdr binary name (overridable with HERDR_BIN_PATH). */
export const DEFAULT_HERDR_BIN = "herdr";

/** Default pane label of the ContextHub Herdr plugin. */
export const DEFAULT_TARGET_PANE_LABEL = "Work Items";

/**
 * Normalise a phrase or transcript for matching: lower-case, collapse
 * whitespace and strip trailing sentence punctuation.
 * @param {unknown} text
 * @returns {string}
 */
export function normalisePhrase(text) {
  return String(text ?? "")
    .toLowerCase()
    .trim()
    .replace(/[.,!?;:'")\]}]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Find the configured shortcut whose phrase matches the transcript.
 * @param {Array<{phrase: string, chord: string[]}>} shortcuts
 * @param {string} transcript
 * @returns {{phrase: string, chord: string[]}|null}
 */
export function matchShortcut(shortcuts, transcript) {
  if (!Array.isArray(shortcuts) || shortcuts.length === 0) return null;
  const normalised = normalisePhrase(transcript);
  if (!normalised) return null;
  for (const shortcut of shortcuts) {
    if (!shortcut || typeof shortcut.phrase !== "string") continue;
    if (normalisePhrase(shortcut.phrase) === normalised) return shortcut;
  }
  return null;
}

/**
 * Resolve the target pane id from a `herdr pane list` payload.
 * An explicit `targetPaneId` always wins; otherwise the pane whose label (or
 * stripped terminal title) matches `targetPaneLabel` is used.
 * @param {Array<object>} panes
 * @param {{targetPaneLabel?: string, targetPaneId?: string}} [options]
 * @returns {string|null}
 */
export function resolveTargetPane(panes, { targetPaneLabel = "", targetPaneId = "" } = {}) {
  if (targetPaneId) return targetPaneId;
  const wanted = normalisePhrase(targetPaneLabel);
  if (!wanted) return null;
  for (const pane of panes || []) {
    if (!pane) continue;
    if (normalisePhrase(pane.label) === wanted) return pane.pane_id || null;
    if (normalisePhrase(pane.terminal_title_stripped) === wanted) return pane.pane_id || null;
  }
  return null;
}

/**
 * Run a Herdr command and parse its JSON output.
 * @param {string[]} args
 * @param {{herdrBin?: string, timeoutMs?: number}} [options]
 * @returns {object}
 */
export function runHerdr(args, { herdrBin = DEFAULT_HERDR_BIN, timeoutMs = 10000 } = {}) {
  const result = spawnSync(herdrBin, args, { encoding: "utf8", timeout: timeoutMs });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim();
    throw new Error(detail || `${herdrBin} ${args.join(" ")} exited with code ${result.status}`);
  }
  const output = (result.stdout || "").trim();
  if (!output) return {};
  try {
    return JSON.parse(output);
  } catch {
    return { raw: output };
  }
}

/**
 * Create the controller `sendShortcut` dependency.
 *
 * @param {object} [options]
 * @param {Array<{phrase: string, chord: string[]}>} [options.shortcuts]
 * @param {string} [options.targetPaneLabel] pane label to target in the workspace
 * @param {string} [options.targetPaneId] explicit pane id (wins over the label)
 * @param {string} [options.workspaceId] `HERDR_WORKSPACE_ID` of the calling pane
 * @param {string} [options.herdrBin] path/name of the herdr binary
 * @param {(args: string[]) => object} [options.run] injectable herdr runner (tests)
 * @param {(message: string, level?: string) => void} [options.notify]
 * @returns {(transcript: string) => Promise<boolean>} true when a shortcut matched
 */
export function createHerdrShortcutRunner({
  shortcuts = [],
  targetPaneLabel = DEFAULT_TARGET_PANE_LABEL,
  targetPaneId = "",
  workspaceId = "",
  herdrBin = DEFAULT_HERDR_BIN,
  run = null,
  notify = () => {},
} = {}) {
  const execute = run || ((args) => runHerdr(args, { herdrBin }));

  return async function sendShortcut(transcript) {
    const shortcut = matchShortcut(shortcuts, transcript);
    if (!shortcut) return false;
    if (!Array.isArray(shortcut.chord) || shortcut.chord.length === 0) return true;

    try {
      let paneId = targetPaneId;
      if (!paneId) {
        const args = ["pane", "list"];
        if (workspaceId) args.push("--workspace", workspaceId);
        const parsed = execute(args);
        paneId = resolveTargetPane(parsed && parsed.result && parsed.result.panes, {
          targetPaneLabel,
          targetPaneId,
        });
      }
      if (!paneId) {
        notify(
          `Voice input: no Herdr pane labelled "${targetPaneLabel}" found; ` +
            `shortcut "${shortcut.phrase}" was not sent.`,
          "warning",
        );
        return true;
      }
      execute(["pane", "send-keys", paneId, ...shortcut.chord]);
      return true;
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      notify(
        `Voice input: failed to send Herdr shortcut "${shortcut.phrase}": ${message}`,
        "warning",
      );
      return true;
    }
  };
}
