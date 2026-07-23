import Link from "next/link";
import { backendClient } from "@/lib/api-client";
import { BuildSquadButton } from "@/components/build-squad-button";
import { SingleFieldActionForm } from "@/components/single-field-action-form";
import { Card, EmptyState, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function TeamPage() {
  const client = backendClient();
  const { data: snapshots, response } = await client.GET("/squads");

  if (!snapshots) {
    return <ErrorState>Could not load squad snapshots ({response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.75rem", fontSize: "1.1rem" }}>Sync your team</h1>
        <SingleFieldActionForm
          path="/managers/sync"
          fieldName="fpl_manager_id"
          fieldLabel="Your FPL manager ID"
          fieldType="number"
          submitLabel="Sync"
        />
        <p style={{ margin: "0.75rem 0 0", fontSize: "0.85rem", color: "var(--text-muted)" }}>
          Or <Link href="/team/new">enter a squad manually</Link> to try a hypothetical team.
        </p>
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Start from scratch</h2>
        <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
          No manager to sync yet? Build a fresh, optimal 15-man squad directly from the solver.
        </p>
        <BuildSquadButton />
      </Card>

      {snapshots.length === 0 ? (
        <EmptyState>No squad has been synced or entered yet.</EmptyState>
      ) : (
        <Card>
          <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Recent squads</h2>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {snapshots.map((snapshot) => (
              <div
                key={snapshot.manager_squad_snapshot_id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "0.5rem 0",
                  borderTop: "1px solid var(--border)",
                  fontSize: "0.9rem",
                }}
              >
                <span>
                  {snapshot.source === "manager_sync" ? "Synced" : "Manual"}
                  {snapshot.is_current && (
                    <span style={{ color: "var(--success)", marginLeft: "0.5rem" }}>current</span>
                  )}
                  <span style={{ color: "var(--text-muted)", marginLeft: "0.5rem" }}>
                    {new Date(snapshot.created_at).toLocaleString()}
                  </span>
                </span>
                <Link href={`/team/${snapshot.manager_squad_snapshot_id}`}>View</Link>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
