import type { components } from "@/lib/api-types";

type DifferentialPlayer = components["schemas"]["SquadDifferentialPlayerOut"];

function PlayerColumn({ title, players }: { title: string; players: DifferentialPlayer[] }) {
  return (
    <div>
      <h3 style={{ margin: "0 0 0.35rem", fontSize: "0.8rem", color: "var(--text-muted)" }}>
        {title} ({players.length})
      </h3>
      {players.length > 0 ? (
        <ul style={{ margin: 0, paddingLeft: "1.1rem", fontSize: "0.85rem" }}>
          {players.map((player) => (
            <li key={player.player_id}>
              {player.web_name ?? "Unknown"}
              {player.position && <span style={{ color: "var(--text-muted)" }}> ({player.position})</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--text-muted)" }}>None -- template picks</p>
      )}
    </div>
  );
}

/**
 * The single most actionable piece of H2H intel: which players you own that this rival
 * doesn't, and vice versa. Both lists come pre-resolved (names/positions) from
 * GET /leagues/{league_id}/rivals -- see that endpoint's own docstring for why both are
 * null together rather than partially populated (no current squad synced yet, or this
 * rival has no AVAILABLE picks snapshot yet).
 */
export function SquadDifferential({
  currentSquadId,
  onlyYouHave,
  onlyRivalHas,
}: {
  currentSquadId: string | undefined;
  onlyYouHave: DifferentialPlayer[] | null;
  onlyRivalHas: DifferentialPlayer[] | null;
}) {
  if (onlyYouHave === null || onlyRivalHas === null) {
    return (
      <p style={{ margin: "0.75rem 0 0", fontSize: "0.8rem", color: "var(--text-muted)" }}>
        {currentSquadId
          ? "Player differential unavailable -- this rival has no synced picks yet."
          : "Sync your own squad to see which players you and this rival don't share."}
      </p>
    );
  }

  return (
    <div style={{ marginTop: "0.75rem" }}>
      <p style={{ margin: "0 0 0.5rem", fontSize: "0.78rem", color: "var(--text-muted)" }}>
        Players you both own aren&apos;t listed on either side -- they don&apos;t affect who wins this
        matchup, so only the differences are shown.
      </p>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: "0.75rem",
        }}
      >
        <PlayerColumn title="Only you have" players={onlyYouHave} />
        <PlayerColumn title="Only they have" players={onlyRivalHas} />
      </div>
    </div>
  );
}
