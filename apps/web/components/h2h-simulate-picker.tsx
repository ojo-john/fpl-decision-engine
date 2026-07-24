"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { browserClient } from "@/lib/browser-client";
import { extractErrorMessage } from "@/components/action-button";
import { buttonStyle, inputStyle } from "@/components/ui";
import type { components } from "@/lib/api-types";

type RivalOption = { rivalProfileId: string; label: string };

/**
 * The Rivals page only ever simulates against the CURRENT synced squad (it already knows a
 * rival_profile_id per row there). Any OTHER squad -- manual, or a past synced snapshot --
 * has no such context on its own detail page, so this component resolves the rival list itself
 * (first synced league, same "first league" assumption app/(app)/rivals/page.tsx already makes)
 * and lets the user pick one before simulating.
 */
export function H2HSimulatePicker({ managerSquadSnapshotId }: { managerSquadSnapshotId: string }) {
  const router = useRouter();
  const [rivals, setRivals] = useState<RivalOption[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedRivalProfileId, setSelectedRivalProfileId] = useState("");
  const [pending, setPending] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadRivals() {
      const client = browserClient();
      const { data: leagues } = await client.GET("/leagues");
      const league = leagues?.[0];
      if (!league) {
        if (!cancelled) setLoadError("No league has been synced yet.");
        return;
      }
      const [{ data: rivalProfiles }, { data: standings }] = await Promise.all([
        client.GET("/leagues/{league_id}/rivals", { params: { path: { league_id: league.league_id } } }),
        client.GET("/leagues/{league_id}/standings", { params: { path: { league_id: league.league_id } } }),
      ]);
      if (cancelled) return;
      if (!rivalProfiles || rivalProfiles.length === 0) {
        setLoadError("No rival profiles computed yet.");
        return;
      }
      const standingByManager = new Map(
        (standings ?? []).map((entry: components["schemas"]["LeagueEntryOut"]) => [
          entry.fpl_manager_id,
          entry,
        ]),
      );
      setRivals(
        rivalProfiles.map((rival) => ({
          rivalProfileId: rival.rival_profile_id,
          label:
            standingByManager.get(rival.fpl_manager_id)?.entry_name ?? `Manager ${rival.fpl_manager_id}`,
        })),
      );
    }
    loadRivals().catch(() => {
      if (!cancelled) setLoadError("Could not reach the server.");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSimulate() {
    if (!selectedRivalProfileId) return;
    setPending(true);
    setSubmitError(null);
    try {
      const { data, error, response } = await browserClient().POST("/h2h/simulate", {
        body: {
          manager_squad_snapshot_id: managerSquadSnapshotId,
          rival_profile_id: selectedRivalProfileId,
        },
      });
      if (!data) {
        setSubmitError(extractErrorMessage(error) ?? `Request failed (${response.status}).`);
        return;
      }
      router.push(`/h2h/${data.h2h_simulation_run_id}`);
    } catch {
      setSubmitError("Could not reach the server.");
    } finally {
      setPending(false);
    }
  }

  if (loadError) {
    return <span style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>{loadError}</span>;
  }
  if (!rivals) {
    return <span style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>Loading rivals...</span>;
  }

  return (
    <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
      <select
        value={selectedRivalProfileId}
        onChange={(event) => setSelectedRivalProfileId(event.target.value)}
        style={inputStyle}
      >
        <option value="">Choose a rival...</option>
        {rivals.map((rival) => (
          <option key={rival.rivalProfileId} value={rival.rivalProfileId}>
            {rival.label}
          </option>
        ))}
      </select>
      <button
        onClick={handleSimulate}
        disabled={!selectedRivalProfileId || pending}
        style={{ ...buttonStyle, opacity: !selectedRivalProfileId || pending ? 0.6 : 1 }}
      >
        {pending ? "Simulating..." : "Simulate H2H"}
      </button>
      {submitError && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{submitError}</span>}
    </div>
  );
}
