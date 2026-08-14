"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { browserClient } from "@/lib/browser-client";
import { extractErrorMessage } from "@/components/action-button";
import { Card, buttonStyle, inputStyle } from "@/components/ui";
import type { components } from "@/lib/api-types";

type RoleNote = components["schemas"]["RoleNoteOut"];

/**
 * Curated, sourced set-piece/role-duty notes (penalties/corners/free-kicks) -- a SEPARATE
 * mechanism from AvailabilityRiskNotes, not a variant of it. Risk notes are a discount
 * (something bad, styled with --danger); role notes are normally a boost (something good,
 * styled with --success here) -- both can be active for the same player at once and both apply,
 * see packages.database.models.player_role_note's own docstring for the full "why two
 * mechanisms" rationale. FPL's own bootstrap-static API has no field for "who takes penalties" --
 * this closes that gap the same way availability-risk notes close the pre-official-injury gap:
 * curated by a human or an agent doing fresh research on request, never automatic.
 */
export function RoleNotes({ playerId, notes }: { playerId: string; notes: RoleNote[] }) {
  const router = useRouter();
  const [role, setRole] = useState("");
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState("1.15");
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
      const { error: createError, response } = await browserClient().POST("/players/{player_id}/role-notes", {
        params: { path: { player_id: playerId } },
        body: {
          role,
          reason,
          severity,
          valid_from_fpl_event_id: validFrom ? Number(validFrom) : null,
          valid_until_fpl_event_id: validUntil ? Number(validUntil) : null,
          created_by: "manual",
        },
      });
      if (!response.ok) {
        setError(extractErrorMessage(createError) ?? `Request failed (${response.status}).`);
        return;
      }
      setRole("");
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
        "/players/{player_id}/role-notes/{note_id}",
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
      <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Set-piece / role notes</h2>
      <p style={{ margin: "0 0 0.75rem", fontSize: "0.8rem", color: "var(--text-muted)" }}>
        Curated, sourced set-piece duty facts (penalties, corners, free-kicks) -- not exposed
        anywhere in FPL&apos;s own data. Add a note when you have a real, sourced reason to expect
        a boosted role (e.g. a player newly confirmed as penalty taker).
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
                border: "1px solid var(--success)",
                fontSize: "0.85rem",
              }}
            >
              <div>
                <div>
                  <strong>{note.role}</strong> -- {note.reason}
                </div>
                <div style={{ color: "var(--text-muted)", marginTop: "0.2rem" }}>
                  severity {note.severity}
                  {note.valid_from_fpl_event_id !== null || note.valid_until_fpl_event_id !== null
                    ? ` -- GW${note.valid_from_fpl_event_id ?? "?"} to GW${note.valid_until_fpl_event_id ?? "open-ended"}`
                    : " -- no gameweek window set"}
                </div>
              </div>
              <button
                onClick={() => handleDelete(note.id)}
                disabled={deletingId === note.id}
                style={{
                  background: "transparent",
                  border: "1px solid var(--border)",
                  borderRadius: "6px",
                  color: "var(--text-muted)",
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
          value={role}
          onChange={(event) => setRole(event.target.value)}
          placeholder="Role (e.g. 'penalties', 'corners', 'free_kicks')"
          required
          style={inputStyle}
        />
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Reason (with a source, e.g. 'Now confirmed penalty taker -- Sky Sports, Aug 2026')"
          required
          style={inputStyle}
        />
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
          <label style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
            Severity (boost, &gt;1.0)
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
            To GW (optional)
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
