"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { browserClient } from "@/lib/browser-client";
import { extractErrorMessage } from "@/components/action-button";
import { buttonStyle, inputStyle } from "@/components/ui";
import type { components } from "@/lib/api-types";

const SQUAD_SIZE = 15;
const POSITIONS = ["GKP", "DEF", "MID", "FWD"];
const CHIP_TYPES = ["wildcard", "freehit", "bboost", "3xc"];

type PickedPlayer = components["schemas"]["PlayerSummary"];

export function ManualSquadForm() {
  const router = useRouter();

  const [search, setSearch] = useState("");
  const [position, setPosition] = useState("");
  const [results, setResults] = useState<PickedPlayer[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selected, setSelected] = useState<Map<string, PickedPlayer>>(new Map());
  const [bank, setBank] = useState("0");
  const [freeTransfers, setFreeTransfers] = useState("1");
  const [activeChip, setActiveChip] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSearching(true);
    setSearchError(null);
    try {
      const { data, response } = await browserClient().GET("/players", {
        params: {
          query: {
            search: search || undefined,
            position: position || undefined,
            page_size: 50,
          },
        },
      });
      if (!data) {
        setSearchError(`Search failed (${response.status}).`);
        return;
      }
      setResults(data.items);
    } catch {
      setSearchError("Could not reach the server.");
    } finally {
      setSearching(false);
    }
  }

  function addPlayer(player: PickedPlayer) {
    if (selected.size >= SQUAD_SIZE || selected.has(player.id)) return;
    setSelected(new Map(selected).set(player.id, player));
  }

  function removePlayer(playerId: string) {
    const next = new Map(selected);
    next.delete(playerId);
    setSelected(next);
  }

  const selectedPlayers = Array.from(selected.values());
  const totalCost = selectedPlayers.reduce((sum, p) => sum + p.now_cost, 0);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setSubmitError(null);
    try {
      const { data, error, response } = await browserClient().POST("/squads/manual", {
        body: {
          fpl_element_ids: selectedPlayers.map((p) => p.fpl_element_id),
          bank: Number(bank),
          free_transfers_available: Number(freeTransfers),
          active_chip: activeChip || null,
        },
      });
      if (!data) {
        setSubmitError(extractErrorMessage(error) ?? `Request failed (${response.status}).`);
        return;
      }
      router.push(`/team/${data.manager_squad_snapshot_id}`);
    } catch {
      setSubmitError("Could not reach the server.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <form
        onSubmit={handleSearch}
        style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}
      >
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search by name"
          style={{ ...inputStyle, flex: 1, minWidth: "160px" }}
        />
        <select value={position} onChange={(event) => setPosition(event.target.value)} style={inputStyle}>
          <option value="">All positions</option>
          {POSITIONS.map((pos) => (
            <option key={pos} value={pos}>
              {pos}
            </option>
          ))}
        </select>
        <button type="submit" disabled={searching} style={buttonStyle}>
          {searching ? "Searching..." : "Search"}
        </button>
      </form>
      {searchError && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{searchError}</span>}

      {results.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
              <th style={{ padding: "0.25rem 0" }}>Pos</th>
              <th>Player</th>
              <th>Team</th>
              <th>Price</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {results.map((player) => (
              <tr key={player.id} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ padding: "0.35rem 0", color: "var(--text-muted)" }}>{player.position ?? "-"}</td>
                <td>{player.web_name}</td>
                <td>{player.team_short_name ?? ""}</td>
                <td>{(player.now_cost / 10).toFixed(1)}</td>
                <td>
                  <button
                    type="button"
                    onClick={() => addPlayer(player)}
                    disabled={selected.has(player.id) || selected.size >= SQUAD_SIZE}
                    style={{ ...buttonStyle, padding: "0.2rem 0.6rem", fontSize: "0.8rem" }}
                  >
                    {selected.has(player.id) ? "Added" : "Add"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div style={{ borderTop: "1px solid var(--border)", paddingTop: "1rem" }}>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>
          Selected squad ({selected.size}/{SQUAD_SIZE}) -- total {(totalCost / 10).toFixed(1)}
        </h2>
        {selectedPlayers.length > 0 && (
          <ul style={{ listStyle: "none", margin: "0 0 1rem", padding: 0, fontSize: "0.85rem" }}>
            {selectedPlayers.map((player) => (
              <li
                key={player.id}
                style={{ display: "flex", justifyContent: "space-between", padding: "0.25rem 0" }}
              >
                <span>
                  {player.position} -- {player.web_name} ({(player.now_cost / 10).toFixed(1)})
                </span>
                <button
                  type="button"
                  onClick={() => removePlayer(player.id)}
                  style={{ background: "none", border: "none", color: "var(--danger)", cursor: "pointer" }}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem", fontSize: "0.85rem", color: "var(--text-muted)" }}>
              Bank
              <input
                type="number"
                value={bank}
                onChange={(event) => setBank(event.target.value)}
                required
                style={{ ...inputStyle, width: "100px" }}
              />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem", fontSize: "0.85rem", color: "var(--text-muted)" }}>
              Free transfers
              <input
                type="number"
                value={freeTransfers}
                onChange={(event) => setFreeTransfers(event.target.value)}
                required
                style={{ ...inputStyle, width: "100px" }}
              />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem", fontSize: "0.85rem", color: "var(--text-muted)" }}>
              Active chip
              <select value={activeChip} onChange={(event) => setActiveChip(event.target.value)} style={inputStyle}>
                <option value="">None</option>
                {CHIP_TYPES.map((chip) => (
                  <option key={chip} value={chip}>
                    {chip}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {submitError && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{submitError}</span>}
          <button
            type="submit"
            disabled={selected.size !== SQUAD_SIZE || submitting}
            style={{ ...buttonStyle, alignSelf: "flex-start", opacity: selected.size !== SQUAD_SIZE ? 0.6 : 1 }}
          >
            {submitting ? "Creating..." : "Create squad"}
          </button>
        </form>
      </div>
    </div>
  );
}
