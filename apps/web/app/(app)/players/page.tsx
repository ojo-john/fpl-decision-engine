import Link from "next/link";
import { backendClient } from "@/lib/api-client";
import { Card, EmptyState, ErrorState, TableScroll, inputStyle, buttonStyle } from "@/components/ui";

export const dynamic = "force-dynamic";

const POSITIONS = ["GKP", "DEF", "MID", "FWD"];
const PAGE_SIZE = 30;

export default async function PlayersPage({
  searchParams,
}: {
  searchParams: Promise<{ search?: string; position?: string; page?: string }>;
}) {
  const params = await searchParams;
  const search = params.search ?? "";
  const position = params.position ?? "";
  const page = Math.max(1, Number(params.page) || 1);

  const client = backendClient();
  const { data, response } = await client.GET("/players", {
    params: {
      query: {
        search: search || undefined,
        position: position || undefined,
        page,
        page_size: PAGE_SIZE,
      },
    },
  });

  if (!data) {
    return <ErrorState>Could not load players ({response.status}).</ErrorState>;
  }

  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  function pageHref(targetPage: number): string {
    const query = new URLSearchParams();
    if (search) query.set("search", search);
    if (position) query.set("position", position);
    query.set("page", String(targetPage));
    return `/players?${query.toString()}`;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.75rem", fontSize: "1.1rem" }}>Players</h1>
        <form
          method="get"
          style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}
        >
          <input
            name="search"
            defaultValue={search}
            placeholder="Search by name"
            style={{ ...inputStyle, flex: 1, minWidth: "160px" }}
          />
          <select name="position" defaultValue={position} style={inputStyle}>
            <option value="">All positions</option>
            {POSITIONS.map((pos) => (
              <option key={pos} value={pos}>
                {pos}
              </option>
            ))}
          </select>
          <button type="submit" style={buttonStyle}>
            Filter
          </button>
        </form>
      </Card>

      {data.items.length === 0 ? (
        <EmptyState>No players match this filter.</EmptyState>
      ) : (
        <Card>
          <TableScroll>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "0.3rem 0" }}>Pos</th>
                  <th>Player</th>
                  <th>Team</th>
                  <th>Price</th>
                  <th>Form</th>
                  <th>Total pts</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((player) => (
                  <tr key={player.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "0.4rem 0", color: "var(--text-muted)" }}>
                      {player.position ?? "-"}
                    </td>
                    <td>
                      <Link href={`/players/${player.id}`}>{player.web_name ?? "Unknown"}</Link>
                      {player.is_quarantined && (
                        <span style={{ color: "var(--danger)", marginLeft: "0.4rem", fontSize: "0.8rem" }}>
                          quarantined
                        </span>
                      )}
                    </td>
                    <td>{player.team_short_name ?? "-"}</td>
                    <td>{(player.now_cost / 10).toFixed(1)}</td>
                    <td>{player.form ?? "-"}</td>
                    <td>{player.total_points}</td>
                    <td>{player.status ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginTop: "0.75rem",
              fontSize: "0.85rem",
              color: "var(--text-muted)",
            }}
          >
            <span>
              Page {page} of {totalPages} ({data.total} players)
            </span>
            <div style={{ display: "flex", gap: "0.75rem" }}>
              {page > 1 && <Link href={pageHref(page - 1)}>Previous</Link>}
              {page < totalPages && <Link href={pageHref(page + 1)}>Next</Link>}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
