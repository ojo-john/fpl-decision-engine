import ReactMarkdown from "react-markdown";
import { backendClient } from "@/lib/api-client";
import { BacktestButton } from "@/components/backtest-button";
import { SingleFieldActionForm } from "@/components/single-field-action-form";
import { Card, EmptyState, ErrorState, TableScroll } from "@/components/ui";

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
      <BacktestButton modelVersionId={champion.model_version_id} />
    </Card>
  );
}

// Snake_case enum values (e.g. "rolling_projection_mae") read fine as prose but not as a table
// cell -- this is a display-only transform, not a validated label map, so it stays correct for
// any future enum member without needing an update here.
function humanize(value: string): string {
  return value.replaceAll("_", " ");
}

async function LearningSignalsSection() {
  const { data: signals } = await backendClient().GET("/learning/signals");
  if (!signals) return null;
  return (
    <Card>
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Learning signals</h2>
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
                <th>Window (GWs)</th>
                <th>Value</th>
                <th>Samples</th>
                <th>Computed</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((signal) => (
                <tr key={signal.learning_signal_id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td style={{ padding: "0.3rem 0" }}>
                    {humanize(signal.signal_type)}
                    {signal.threshold_breached && (
                      <span style={{ color: "var(--danger)", marginLeft: "0.4rem" }}>breach</span>
                    )}
                  </td>
                  <td>{signal.window_size_gameweeks}</td>
                  <td>{Number(signal.value).toFixed(3)}</td>
                  <td>{signal.sample_count}</td>
                  <td>{new Date(signal.computed_at).toLocaleDateString()}</td>
                </tr>
              ))}
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
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Failure attributions</h2>
      {failures.length === 0 ? (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
          No material misses have been attributed to a cause yet.
        </p>
      ) : (
        <TableScroll>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                <th style={{ padding: "0.3rem 0" }}>Reason</th>
                <th>Category</th>
                <th>Severity</th>
                <th>Recorded</th>
              </tr>
            </thead>
            <tbody>
              {failures.map((failure) => (
                <tr key={failure.failure_attribution_id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td style={{ padding: "0.3rem 0" }}>{humanize(failure.reason_code)}</td>
                  <td>{humanize(failure.category)}</td>
                  <td>{failure.severity !== null ? Number(failure.severity).toFixed(2) : "--"}</td>
                  <td>{new Date(failure.created_at).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>
          Gameweek {data.target_fpl_event_id} review -- {data.run_status}
        </h1>
        {data.retraining_triggered && (
          <p style={{ margin: 0, color: "var(--accent)", fontSize: "0.9rem" }}>
            A sustained projection-accuracy breach triggered model retraining on this run.
          </p>
        )}
      </Card>

      {reviewForm}
      <ChampionBacktestSection />
      <LearningSignalsSection />
      <FailureAttributionsSection />

      {data.report_markdown ? (
        <Card>
          <div className="markdown-body">
            <ReactMarkdown>{data.report_markdown}</ReactMarkdown>
          </div>
        </Card>
      ) : (
        <EmptyState>This run has no rendered report yet.</EmptyState>
      )}
    </div>
  );
}
