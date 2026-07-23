import Link from "next/link";
import { backendClient } from "@/lib/api-client";
import { ActionButton } from "@/components/action-button";
import { Card, ConfidenceBadge, EmptyState, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const client = backendClient();
  const { data, response } = await client.GET("/recommendations/latest");

  if (response.status === 404) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        <EmptyState>No recommendation has been generated for the current season yet.</EmptyState>
        <ActionButton
          path="/recommendations/generate"
          label="Generate recommendation"
          pendingLabel="Generating..."
        />
      </div>
    );
  }
  if (!data) {
    return <ErrorState>Could not load the latest recommendation ({response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
          <div>
            <h1 style={{ margin: "0 0 0.25rem", fontSize: "1.1rem" }}>
              Gameweek {data.target_fpl_event_id} recommendation
            </h1>
            <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.85rem" }}>
              Generated {new Date(data.generated_at).toLocaleString()}
            </p>
          </div>
          <ConfidenceBadge confidence={data.confidence} />
        </div>
      </Card>

      <ActionButton
        path="/recommendations/generate"
        label="Regenerate recommendation"
        pendingLabel="Generating..."
      />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "1rem" }}>
        {data.optimization && (
          <Card>
            <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Squad / lineup</h2>
            <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
              {data.optimization.summary}
            </p>
            <Link href={`/squad/${data.optimization.run_id}`}>View squad</Link>
          </Card>
        )}
        {data.transfer_optimization && (
          <Card>
            <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Transfers</h2>
            <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
              {data.transfer_optimization.summary}
            </p>
            <Link href={`/transfers/${data.transfer_optimization.run_id}`}>View scenarios</Link>
          </Card>
        )}
        {data.h2h_simulation && (
          <Card>
            <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>H2H matchup</h2>
            <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
              {data.h2h_simulation.summary}
            </p>
            <Link href={`/h2h/${data.h2h_simulation.run_id}`}>View matchup</Link>
          </Card>
        )}
        {data.chip_optimization && (
          <Card>
            <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Chips</h2>
            <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
              {data.chip_optimization.summary}
            </p>
            <Link href={`/chips/${data.chip_optimization.run_id}`}>View recommendations</Link>
          </Card>
        )}
      </div>

      {data.confidence_drivers.length > 0 && (
        <Card>
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Confidence drivers</h2>
          <ul style={{ margin: 0, paddingLeft: "1.1rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
            {data.confidence_drivers.map((driver, i) => (
              <li key={i}>{driver}</li>
            ))}
          </ul>
        </Card>
      )}

      {data.known_risks.length > 0 && (
        <Card>
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Known risks</h2>
          <ul style={{ margin: 0, paddingLeft: "1.1rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
            {data.known_risks.map((risk, i) => (
              <li key={i}>{risk}</li>
            ))}
          </ul>
        </Card>
      )}

      {data.assumptions.length > 0 && (
        <Card>
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Assumptions</h2>
          <ul style={{ margin: 0, paddingLeft: "1.1rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
            {data.assumptions.map((assumption, i) => (
              <li key={i}>{assumption}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
