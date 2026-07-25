"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { browserClient } from "@/lib/browser-client";
import { extractErrorMessage } from "@/components/action-button";
import { Card, buttonStyle, inputStyle } from "@/components/ui";
import type { components } from "@/lib/api-types";

type AvailabilityRiskNote = components["schemas"]["AvailabilityRiskNoteOut"];

/**
 * Judgment-based fitness/availability risk notes -- distinct from FPL's own official
 * injury/doubt data (already reflected elsewhere via chance_of_playing_next_round), this is for
 * a real, sourced risk FPL hasn't flagged yet (e.g. a player short on match fitness after a long
 * international tournament). See packages.database.models.player_availability_risk_note's own
 * docstring for the full rationale. Every note here is curated by a human (or an agent doing
 * fresh research on request) -- there is no automatic news-monitoring in this system.
 */
export function AvailabilityRiskNotes({
  playerId,
  notes,
}: {
  playerId: string;
  notes: AvailabilityRiskNote[];
}) {
  const router = useRouter();
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState("0.7");
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const { error: createError, response } = await browserClient().POST(
        "/players/{player_id}/availability-risk-notes",
        {
          params: { path: { player_id: playerId } },
          body: {
            reason,
            severity,
            valid_from_fpl_event_id: validFrom ? Number(validFrom) : null,
            valid_until_fpl_event_id: validUntil ? Number(validUntil) : null,
            created_by: "manual",
          },
        },
      );
      if (!response.ok) {
        setError(extractErrorMessage(createError) ?? `Request failed (${response.status}).`);
        return;
      }
      setReason("");
      setValidFrom("");
      setValidUntil("");
      router.refresh();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
    }
  }

  async function handleDelete(noteId: string) {
    setDeletingId(noteId);
    setError(null);
    try {
      const { error: deleteError, response } = await browserClient().DELETE(
        "/players/{player_id}/availability-risk-notes/{note_id}",
        { params: { path: { player_id: playerId, note_id: noteId } } },
      );
      if (!response.ok) {
        setError(extractErrorMessage(deleteError) ?? `Delete failed (${response.status}).`);
        return;
      }
      router.refresh();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <Card>
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Availability risk notes</h2>
      <p style={{ margin: "0 0 0.75rem", fontSize: "0.8rem", color: "var(--text-muted)" }}>
        Curated, judgment-based fitness/availability risk -- not FPL&apos;s own official
        injury/doubt data (that already flows in automatically). Add a note when you have a real,
        sourced reason to expect reduced involvement that FPL hasn&apos;t flagged yet.
      </p>

      {notes.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "0.75rem" }}>
          {notes.map((note) => (
            <div
              key={note.id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                gap: "0.75rem",
                padding: "0.5rem 0.6rem",
                borderRadius: "6px",
                border: "1px solid var(--border)",
                fontSize: "0.85rem",
              }}
            >
              <div>
                <div>{note.reason}</div>
                <div style={{ color: "var(--text-muted)", marginTop: "0.2rem" }}>
                  severity {note.severity}
                  {note.valid_from_fpl_event_id !== null || note.valid_until_fpl_event_id !== null
                    ? ` -- GW${note.valid_from_fpl_event_id ?? "?"} to GW${note.valid_until_fpl_event_id ?? "?"}`
                    : " -- no gameweek window set"}
                </div>
              </div>
              <button
                onClick={() => handleDelete(note.id)}
                disabled={deletingId === note.id}
                style={{
                  background: "transparent",
                  border: "1px solid var(--danger)",
                  borderRadius: "6px",
                  color: "var(--danger)",
                  padding: "0.25rem 0.5rem",
                  fontSize: "0.8rem",
                  cursor: "pointer",
                  flexShrink: 0,
                }}
              >
                {deletingId === note.id ? "Removing..." : "Remove"}
              </button>
            </div>
          ))}
        </div>
      )}

      <form onSubmit={handleCreate} style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Reason (with a source, e.g. 'Confirmed late return to training -- Arsenal reporting, July 2026')"
          required
          style={inputStyle}
        />
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
          <label style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
            Severity (0-1)
            <input
              value={severity}
              onChange={(event) => setSeverity(event.target.value)}
              required
              style={{ ...inputStyle, width: "80px", marginLeft: "0.4rem" }}
            />
          </label>
          <label style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
            From GW
            <input
              value={validFrom}
              onChange={(event) => setValidFrom(event.target.value)}
              type="number"
              style={{ ...inputStyle, width: "70px", marginLeft: "0.4rem" }}
            />
          </label>
          <label style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
            To GW
            <input
              value={validUntil}
              onChange={(event) => setValidUntil(event.target.value)}
              type="number"
              style={{ ...inputStyle, width: "70px", marginLeft: "0.4rem" }}
            />
          </label>
          <button type="submit" disabled={pending} style={{ ...buttonStyle, opacity: pending ? 0.7 : 1 }}>
            {pending ? "Adding..." : "Add note"}
          </button>
        </div>
        {error && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{error}</span>}
      </form>
    </Card>
  );
}
