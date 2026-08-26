import ReactMarkdown from "react-markdown";
import { backendClient } from "@/lib/api-client";
import { BacktestButton } from "@/components/backtest-button";
import { RetrainButton } from "@/components/retrain-button";
import { SingleFieldActionForm } from "@/components/single-field-action-form";
import { Card, EmptyState, ErrorState, TableScroll } from "@/components/ui";
import {
  describeFailureEvidence,
  FAILURE_CATEGORY_LABELS,
  FAILURE_REASON_LABELS,
  formatSignalReading,
  RUN_STATUS_LABELS,
  SIGNAL_LABELS,
} from "@/lib/learning-labels";

export const dynamic = "force-dynamic";

const reviewForm = (
  <SingleFieldActionForm
    path="/jobs/review-gameweek"
    fieldName="target_fpl_event_id"
    fieldLabel="Gameweek to review"
    fieldType="number"
    submitLabel="Run review"
  />
);

async function ChampionBacktestSection() {
  const { data: champion } = await backendClient().GET("/models/champion");
  if (!champion) return null;
  return (
    <Card>
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Champion model</h2>
      <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
        {champion.model_name} ({champion.model_family}, feature v{champion.feature_version})
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
        <BacktestButton modelVersionId={champion.model_version_id} />
        <RetrainButton />
      </div>
    </Card>
  );
}

function CategoryPill({ category }: { category: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "0.1rem 0.5rem",
        borderRadius: "999px",
        fontSize: "0.72rem",
        color: "var(--text-muted)",
        border: "1px solid var(--border)",
        whiteSpace: "nowrap",
      }}
    >
      {FAILURE_CATEGORY_LABELS[category] ?? category.replaceAll("_", " ")}
    </span>
  );
}

async function LearningSignalsSection() {
  const { data: signals } = await backendClient().GET("/learning/signals");
  if (!signals) return null;
  return (
    <Card>
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Learning signals</h2>
      <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
        Rolling checks on how well recommendations have been doing lately.
      </p>
      {signals.length === 0 ? (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
          No rolling-window signals computed yet.
        </p>
      ) : (
        <TableScroll>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                <th style={{ padding: "0.3rem 0" }}>Signal</th>
                <th>Reading</th>
                <th>Computed</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((signal) => {
                const meta = SIGNAL_LABELS[signal.signal_type];
                const reading = formatSignalReading(
                  signal.signal_type,
                  Number(signal.value),
                  signal.sample_count,
                  signal.threshold_config_snapshot
                );
                return (
                  <tr key={signal.learning_signal_id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "0.5rem 0", verticalAlign: "top" }}>
                      <div>{meta?.label ?? signal.signal_type.replaceAll("_", " ")}</div>
                      {meta?.description && (
                        <div style={{ color: "var(--text-muted)", fontSize: "0.78rem", marginTop: "0.15rem" }}>
                          {meta.description}
                        </div>
                      )}
                    </td>
                    <td style={{ verticalAlign: "top" }}>
                      <div>
                        {reading.headline}
                        {signal.threshold_breached && (
                          <span style={{ color: "var(--danger)", marginLeft: "0.4rem" }}>-- alert</span>
                        )}
                      </div>
                      {reading.detail && (
                        <div style={{ color: "var(--text-muted)", fontSize: "0.78rem", marginTop: "0.15rem" }}>
                          {reading.detail}
                        </div>
                      )}
                    </td>
                    <td style={{ verticalAlign: "top" }}>{new Date(signal.computed_at).toLocaleDateString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Card>
  );
}

async function FailureAttributionsSection() {
  const { data: failures } = await backendClient().GET("/learning/failures");
  if (!failures) return null;
  return (
    <Card>
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>What&apos;s gone wrong</h2>
      <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
        Specific mistakes the system has found by comparing its own recommendations against what
        actually happened.
      </p>
      {failures.length === 0 ? (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
          No material misses have been attributed to a cause yet.
        </p>
      ) : (
        <TableScroll>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                <th style={{ padding: "0.3rem 0" }}>What happened</th>
                <th>Recorded</th>
              </tr>
            </thead>
            <tbody>
              {failures.map((failure) => {
                const detail = describeFailureEvidence(failure.reason_code, failure.evidence);
                return (
                  <tr key={failure.failure_attribution_id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "0.5rem 0", verticalAlign: "top" }}>
                      <div style={{ display: "flex", gap: "0.5rem", alignItems: "baseline", flexWrap: "wrap" }}>
                        <span>{FAILURE_REASON_LABELS[failure.reason_code] ?? failure.reason_code.replaceAll("_", " ")}</span>
                        <CategoryPill category={failure.category} />
                      </div>
                      {detail && (
                        <div style={{ color: "var(--text-muted)", fontSize: "0.78rem", marginTop: "0.2rem" }}>
                          {detail}
                        </div>
                      )}
                    </td>
                    <td style={{ verticalAlign: "top" }}>{new Date(failure.created_at).toLocaleDateString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Card>
  );
}

function describeFinding(finding: Record<string, unknown>): { text: string; detail?: string } {
  const type = finding.type;
  if (type === "material_failure") {
    const reasonCode = String(finding.reason_code ?? "");
    const label = FAILURE_REASON_LABELS[reasonCode] ?? reasonCode.replaceAll("_", " ");
    const detail = describeFailureEvidence(reasonCode, finding.evidence as Record<string, unknown> | null);
    return { text: label, detail: detail ?? undefined };
  }
  if (type === "signal_breached") {
    const signalType = String(finding.signal_type ?? "");
    const meta = SIGNAL_LABELS[signalType];
    const value = Number(finding.value);
    const sampleCount = Number(finding.sample_count ?? 0);
    const reading = Number.isFinite(value) ? formatSignalReading(signalType, value, sampleCount, null) : null;
    return {
      text: `${meta?.label ?? signalType.replaceAll("_", " ")} crossed its alert threshold`,
      detail: reading?.headline,
    };
  }
  if (type === "retraining_triggered") {
    const alertSent = finding.alert_sent === true;
    return {
      text: "A sustained drop in prediction accuracy triggered a retraining alert",
      detail: alertSent ? "The alert was sent." : "The alert failed to send.",
    };
  }
  if (type === "note_effectiveness_negative") {
    return {
      text: "A note applied to a player's projection made it less accurate, not more",
      detail: typeof finding.player_id === "string" ? `Player: ${finding.player_id}` : undefined,
    };
  }
  return { text: String(type ?? "Unrecognized finding") };
}

function NotableFindingsSection({ findings }: { findings: Record<string, unknown>[] | null }) {
  return (
    <Card>
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>What happened this gameweek</h2>
      {!findings || findings.length === 0 ? (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
          Nothing worth flagging -- the recommendation held up.
        </p>
      ) : (
        <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {findings.map((finding, index) => {
            const { text, detail } = describeFinding(finding);
            return (
              <li key={index} style={{ fontSize: "0.9rem" }}>
                {text}
                {detail && (
                  <span style={{ color: "var(--text-muted)", fontSize: "0.82rem" }}> -- {detail}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

export default async function LearningPage() {
  const client = backendClient();
  const { data, response } = await client.GET("/learning/latest");

  if (response.status === 404) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        <EmptyState>No gameweek review has run for the current season yet.</EmptyState>
        {reviewForm}
        <ChampionBacktestSection />
        <LearningSignalsSection />
        <FailureAttributionsSection />
      </div>
    );
  }
  if (!data) {
    return <ErrorState>Could not load the latest learning report ({response.status}).</ErrorState>;
  }

  // Milestone 7 Layer 6, revised 2026-08-25: retraining_triggered now means "an MAE-breach push
  // alert was attempted", not "a MODEL_TRAINING job ran" -- see packages/database/models
  // /learning_report.py's own docstring. Whether the alert itself sent is on summary.alert_sent
  // (summary is a loosely-typed dict, same as notable_findings -- see LearningReportOut).
  const retrainingAlertSent = data.summary?.retraining_alert_sent === true;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>
          Gameweek {data.target_fpl_event_id} review -- {RUN_STATUS_LABELS[data.run_status] ?? data.run_status}
        </h1>
        {data.retraining_triggered && (
          <p
            style={{
              margin: 0,
              color: retrainingAlertSent ? "var(--accent)" : "var(--danger, #d33)",
              fontSize: "0.9rem",
            }}
          >
            {retrainingAlertSent
              ? "Predictions have been off by more than usual for a while -- a retraining alert was sent. Retraining itself is a manual step (see below)."
              : "Predictions have been off by more than usual for a while, but the retraining alert failed to send."}
          </p>
        )}
      </Card>

      <NotableFindingsSection findings={data.notable_findings} />

      {reviewForm}
      <ChampionBacktestSection />
      <LearningSignalsSection />
      <FailureAttributionsSection />

      {data.report_markdown ? (
        <Card>
          <details>
            <summary style={{ cursor: "pointer", fontSize: "0.9rem", color: "var(--text-muted)" }}>
              Show full technical report
            </summary>
            <div className="markdown-body" style={{ marginTop: "0.75rem" }}>
              <ReactMarkdown>{data.report_markdown}</ReactMarkdown>
            </div>
          </details>
        </Card>
      ) : (
        <EmptyState>This run has no rendered report yet.</EmptyState>
      )}
    </div>
  );
}
