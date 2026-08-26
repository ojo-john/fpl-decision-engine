/** Plain-English translations for the Learning page's own enum-heavy backend vocabulary.
 *
 * These enums (LearningSignalType, FailureCategory, FailureReasonCode,
 * CounterfactualComparisonType) are closed sets defined in packages/domain/learning.py -- see
 * that module's own docstrings for the exact detection rule each FailureReasonCode encodes and
 * app/learning/signals.py for what each signal's "value" actually measures. A raw snake_case
 * dump of these (e.g. "transfer_decision_suboptimal", "rolling_projection_mae 2.447") is what a
 * real user reported not understanding -- these tables exist so the UI can show the underlying
 * finding in a sentence instead.
 */

type Evidence = Record<string, unknown> | null | undefined;

export const FAILURE_CATEGORY_LABELS: Record<string, string> = {
  data: "Data issue",
  minutes_availability: "Playing time",
  performance_model: "Prediction accuracy",
  optimization: "Squad/transfer/chip decision",
  h2h: "Head-to-head prediction",
  variance: "Unpredictable",
};

export const FAILURE_REASON_LABELS: Record<string, string> = {
  fpl_api_degraded_or_blocked: "FPL's own data was unreliable when this recommendation was generated",
  recommendation_not_followed: "The recommended pick wasn't the one played, and it would have scored more",
  projection_overestimated: "The captain was projected to score more than they actually did",
  projection_underestimated: "The captain scored more than they were projected to",
  player_rotated_or_unavailable: "The recommended captain didn't play (rotated or unavailable)",
  rival_prediction_error: "Misjudged who your rival would captain",
  chip_timing_suboptimal: "The chip that was played didn't pay off compared to holding it",
  transfer_decision_suboptimal: "This transfer cost points rather than gained them",
  variance_unexplained: "This pick underperformed with no clear, specific cause -- sometimes football is just unpredictable",
  recommendation_was_sound: "No mistake found -- the recommendation held up",
};

const COMPARISON_LABELS: Record<string, string> = {
  recommended_vs_actual_team: "the recommended team",
  recommended_vs_actual_captain: "the recommended captain",
  transfer_vs_no_transfer: "not making the transfer",
  transfer_vs_top_alternative: "the best alternative transfer",
  hit_vs_no_hit: "not taking the hit",
  chip_vs_hold: "holding the chip",
  overall_vs_h2h_adjusted: "the H2H-adjusted plan",
  conservative_vs_aggressive: "the aggressive alternative",
};

/** A one-line, evidence-backed detail sentence for a failure attribution, when the evidence
 * dict has the shape app/learning/attribution.py's own classifiers are documented to produce for
 * that reason_code. Returns null (render nothing extra) for any shape that doesn't match --
 * evidence is a loosely-typed dict by design (LearningReportOut/FailureAttributionOut), so this
 * stays defensive rather than assuming a field is present.
 */
export function describeFailureEvidence(reasonCode: string, evidence: Evidence): string | null {
  if (!evidence) return null;
  const num = (key: string): number | null => {
    const raw = evidence[key];
    return typeof raw === "number" ? raw : null;
  };

  switch (reasonCode) {
    case "recommendation_not_followed":
    case "transfer_decision_suboptimal":
    case "chip_timing_suboptimal": {
      const delta = num("delta");
      if (delta === null) return null;
      const comparisonType = String(evidence.counterfactual_comparison_type ?? "");
      const comparisonLabel = COMPARISON_LABELS[comparisonType] ?? "the alternative";
      return `Cost about ${Math.abs(delta).toFixed(1)} pts vs. ${comparisonLabel}.`;
    }
    case "projection_overestimated":
    case "projection_underestimated": {
      const projected = num("projected_points");
      const actual = num("actual_raw_points");
      if (projected === null || actual === null) return null;
      return `Projected ${projected.toFixed(1)} pts, actually scored ${actual.toFixed(1)} pts.`;
    }
    case "player_rotated_or_unavailable": {
      const projected = num("projected_points");
      if (projected === null) return null;
      return `Was projected for ${projected.toFixed(1)} pts but played 0 minutes.`;
    }
    case "rival_prediction_error": {
      const probability = num("predicted_probability");
      if (probability === null) return null;
      return `Predicted with ${(probability * 100).toFixed(0)}% confidence -- your rival picked someone else.`;
    }
    case "fpl_api_degraded_or_blocked": {
      const health = evidence.fpl_api_health_at_generation;
      return typeof health === "string" ? `FPL's own data was reported "${health}" at the time.` : null;
    }
    default:
      return null;
  }
}

export interface SignalMeta {
  label: string;
  description: string;
}

export const SIGNAL_LABELS: Record<string, SignalMeta> = {
  rolling_projection_mae: {
    label: "Captain prediction accuracy",
    description: "How many points off, on average, the captain projection has been recently.",
  },
  rolling_h2h_win_probability_calibration: {
    label: "H2H win-chance accuracy",
    description: "Whether predicted head-to-head win chances have matched what actually happened.",
  },
  rolling_chip_recommendation_value: {
    label: "Chip advice track record",
    description: "Whether chip recommendations have actually paid off vs. holding, so far this season. Informational only.",
  },
  failure_reason_concentration: {
    label: "Repeat-mistake check",
    description: "Whether recent misses share the same underlying cause, pointing to one fixable issue rather than scattered bad luck.",
  },
};

/** A plain-language reading of a signal's value, in the unit that signal_type actually measures
 * -- see app/learning/signals.py's own compute_* docstrings (MAE in points, Brier score for
 * calibration, mean points-per-play for chip value, a share of recent batches for concentration).
 */
export function formatSignalReading(
  signalType: string,
  value: number,
  sampleCount: number,
  thresholdConfigSnapshot: Record<string, unknown> | null | undefined
): { headline: string; detail?: string } {
  if (sampleCount === 0) {
    return { headline: "Not enough data yet" };
  }
  switch (signalType) {
    case "rolling_projection_mae":
      return {
        headline: `${value.toFixed(1)} pts off, on average`,
        detail: `Based on the last ${sampleCount} gameweek${sampleCount === 1 ? "" : "s"}.`,
      };
    case "rolling_h2h_win_probability_calibration":
      return {
        headline: `${value.toFixed(2)} calibration score`,
        detail: "0 = perfectly calibrated, 0.25 ≈ a random guess. Lower is better.",
      };
    case "rolling_chip_recommendation_value": {
      const sign = value >= 0 ? "+" : "";
      return {
        headline: `${sign}${value.toFixed(1)} pts per chip played, on average, vs. holding it`,
        detail: `Based on ${sampleCount} chip play${sampleCount === 1 ? "" : "s"} this season.`,
      };
    }
    case "failure_reason_concentration": {
      const topCode = thresholdConfigSnapshot?.most_concentrated_reason_code;
      const topLabel =
        typeof topCode === "string" ? FAILURE_REASON_LABELS[topCode] ?? topCode.replaceAll("_", " ") : null;
      return {
        headline: `${(value * 100).toFixed(0)}% of recent misses share the same cause`,
        detail: topLabel ? `Most common: ${topLabel}.` : undefined,
      };
    }
    default:
      return { headline: value.toFixed(3) };
  }
}

export const RUN_STATUS_LABELS: Record<string, string> = {
  succeeded: "Complete",
  running: "In progress",
  failed: "Failed",
};
