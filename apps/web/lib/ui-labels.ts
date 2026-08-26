/** Small label dictionaries shared across pages -- kept separate from learning-labels.ts, which
 * is specific to the Learning page's own domain vocabulary (signal types, failure reasons). */

export const RUN_STATUS_LABELS: Record<string, string> = {
  succeeded: "Complete",
  running: "In progress",
  failed: "Failed",
};

export const TRANSFER_ACTION_LABELS: Record<string, string> = {
  roll: "Roll (no transfers)",
  one_transfer: "One transfer",
  two_transfers: "Two transfers",
};
