import { backendClient } from "@/lib/api-client";
import { Card, EmptyState, ErrorState, TableScroll } from "@/components/ui";

export const dynamic = "force-dynamic";

const CHIP_LABELS: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  "3xc": "Triple Captain",
};

const STATUS_LABELS: Record<string, string> = {
  open: "Open now",
  upcoming: "Upcoming",
  past: "Past",
};

const STATUS_COLORS: Record<string, string> = {
  open: "var(--success)",
  upcoming: "var(--text-muted)",
  past: "var(--text-muted)",
};

/**
 * Read-only index of this season's chip-scheduling windows -- genuinely available pre-season
 * (see GET /optimize/chips/windows's own docstring), unlike a real chip-simulation run
 * (/chips/[runId]), which needs a synced/manual squad and passes through the optimization gate.
 * Distinct from that dynamic route: this is the overview, not a specific run's results.
 */
export default async function ChipWindowsPage() {
  const client = backendClient();
  const { data, response } = await client.GET("/optimize/chips/windows");

  if (response.status === 404) {
    return (
      <EmptyState>
        No rules config snapshot exists yet for the current season -- run data ingestion first.
      </EmptyState>
    );
  }
  if (!data) {
    return <ErrorState>Could not load chip windows ({response.status}).</ErrorState>;
  }
  if (data.length === 0) {
    return <EmptyState>No chip windows found for the current season.</EmptyState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>Chip windows this season</h1>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
          When each chip can be played, straight from FPL&rsquo;s own published schedule -- this
          is known before a ball is kicked, so it&rsquo;s shown here even while official chip
          simulation is still blocked pre-season.
        </p>
      </Card>

      <Card>
        <TableScroll>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                <th style={{ padding: "0.3rem 0" }}>Chip</th>
                <th>Window</th>
                <th>Gameweeks</th>
                <th>Deadlines</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((window) => (
                <tr
                  key={`${window.chip_type}-${window.window_number}`}
                  style={{ borderTop: "1px solid var(--border)" }}
                >
                  <td style={{ padding: "0.4rem 0" }}>{CHIP_LABELS[window.chip_type] ?? window.chip_type}</td>
                  <td>{window.window_number}</td>
                  <td>
                    GW{window.start_event}
                    {window.stop_event !== window.start_event ? `-${window.stop_event}` : ""}
                  </td>
                  <td style={{ color: "var(--text-muted)" }}>
                    {window.start_deadline ? new Date(window.start_deadline).toLocaleDateString() : "-"}
                    {window.stop_deadline
                      ? ` -- ${new Date(window.stop_deadline).toLocaleDateString()}`
                      : ""}
                  </td>
                  <td style={{ color: STATUS_COLORS[window.status] ?? "var(--text-muted)" }}>
                    {STATUS_LABELS[window.status] ?? window.status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </Card>
    </div>
  );
}
