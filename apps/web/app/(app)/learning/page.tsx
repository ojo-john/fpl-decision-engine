import ReactMarkdown from "react-markdown";
import { backendClient } from "@/lib/api-client";
import { Card, EmptyState, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function LearningPage() {
  const client = backendClient();
  const { data, response } = await client.GET("/learning/latest");

  if (response.status === 404) {
    return (
      <EmptyState>
        No gameweek review has run for the current season yet -- run{" "}
        <code>POST /jobs/review-gameweek</code>.
      </EmptyState>
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
