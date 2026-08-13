"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ActionButton } from "@/components/action-button";
import type { components } from "@/lib/api-types";

type DataRefreshOut = components["schemas"]["DataRefreshOut"];

/**
 * POST /data/refresh -- an on-demand trigger for DATA_INGESTION. Exists
 * specifically so a manager can pull fresh player/price/fixture data right
 * before checking their squad, rather than relying on a background schedule
 * this app has no way to run unattended (see ARCHITECTURE.md's "League
 * Watch" section for the same constraint on a different feature -- nothing
 * outside a live session can reach this app's local database). Confirmed
 * live this session: data had gone 19 days stale mid-transfer-window, and
 * missed 23 real new signings until a manual refresh caught up.
 */
export function DataRefreshButton() {
  const router = useRouter();
  const [result, setResult] = useState<DataRefreshOut | null>(null);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <ActionButton
        path="/data/refresh"
        label="Refresh player data"
        pendingLabel="Refreshing..."
        onSuccess={(data) => {
          setResult(data as DataRefreshOut);
          router.refresh();
        }}
      />
      {result && (
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--text-muted)" }}>
          Refreshed{result.season_label ? ` for ${result.season_label}` : ""} --{" "}
          {result.players_inserted > 0 && `${result.players_inserted} new player(s), `}
          {result.players_updated} updated
          {result.data_quality_score !== null && `, data quality ${result.data_quality_score}/100`}
          {result.status === "succeeded_with_warnings" && ` (${result.issue_count} warning(s))`}.
        </p>
      )}
    </div>
  );
}
