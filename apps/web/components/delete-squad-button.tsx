"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { browserClient } from "@/lib/browser-client";
import { extractErrorMessage } from "@/components/action-button";

/** Only ever rendered for a MANUAL_ENTRY squad -- MANAGER_SYNC rows are an insert-only audit
 * trail with no delete route at all (see apps/api/routers/managers.py's own docstring). */
export function DeleteSquadButton({ snapshotId }: { snapshotId: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    if (!window.confirm("Delete this squad? This cannot be undone.")) return;
    setPending(true);
    setError(null);
    try {
      const { error: deleteError, response } = await browserClient().DELETE(
        "/squads/{manager_squad_snapshot_id}",
        { params: { path: { manager_squad_snapshot_id: snapshotId } } },
      );
      if (!response.ok) {
        setError(extractErrorMessage(deleteError) ?? `Delete failed (${response.status}).`);
        return;
      }
      router.push("/team");
      router.refresh();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", alignItems: "flex-end" }}>
      <button
        onClick={handleClick}
        disabled={pending}
        style={{
          background: "transparent",
          border: "1px solid var(--danger)",
          borderRadius: "6px",
          color: "var(--danger)",
          padding: "0.4rem 0.75rem",
          fontSize: "0.85rem",
          cursor: "pointer",
          opacity: pending ? 0.7 : 1,
        }}
      >
        {pending ? "Deleting..." : "Delete squad"}
      </button>
      {error && <span style={{ color: "var(--danger)", fontSize: "0.8rem" }}>{error}</span>}
    </div>
  );
}
