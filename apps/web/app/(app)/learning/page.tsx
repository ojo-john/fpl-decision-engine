import ReactMarkdown from "react-markdown";
import { backendClient } from "@/lib/api-client";
import { BacktestButton } from "@/components/backtest-button";
import { SingleFieldActionForm } from "@/components/single-field-action-form";
import { Card, EmptyState, ErrorState } from "@/components/ui";

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

export default async function LearningPage() {
  const client = backendClient();
  const { data, response } = await client.GET("/learning/latest");

  if (response.status === 404) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        <EmptyState>No gameweek review has run for the current season yet.</EmptyState>
        {reviewForm}
        <ChampionBacktestSection />
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
