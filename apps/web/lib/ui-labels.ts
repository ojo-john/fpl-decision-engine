/** Small label dictionaries shared across pages -- kept separate from learning-labels.ts, which
 * is specific to the Learning page's own domain vocabulary (signal types, failure reasons). */

export const RUN_STATUS_LABELS: Record<string, string> = {
  succeeded: "Complete",
  running: "In progress",
  failed: "Failed",
};

// Matches apps/api/routers/optimization.py's _recommended_action_for_transfers_count -- scenarios
// are evaluated up to that module's own MAX_TRANSFERS_TO_EVALUATE_CEILING (5), not a fixed 0/1/2.
export const TRANSFER_ACTION_LABELS: Record<string, string> = {
  roll: "Roll (no transfers)",
  one_transfer: "One transfer",
  two_transfers: "Two transfers",
  three_transfers: "Three transfers",
  four_transfers: "Four transfers",
  five_transfers: "Five transfers",
};

const TRANSFERS_COUNT_WORDS: Record<number, string> = { 2: "two", 3: "three", 4: "four", 5: "five" };

/** Mirrors apps/api/routers/optimization.py's _recommended_action_for_transfers_count -- the key
 * to look up in TRANSFER_ACTION_LABELS for a given scenario's transfer count. Real bug fixed
 * 2026-10-06: a scenario-count-based ternary here used to map ANY count >= 2 to "two_transfers",
 * mislabeling a real 3+-transfer scenario as "Two transfers" once scenario_search started
 * evaluating beyond 2 (see that module's own docstring). */
export function transfersCountActionKey(transfersCount: number): string {
  if (transfersCount === 0) return "roll";
  if (transfersCount === 1) return "one_transfer";
  const word = TRANSFERS_COUNT_WORDS[transfersCount];
  return word ? `${word}_transfers` : `${transfersCount}_transfers`;
}
