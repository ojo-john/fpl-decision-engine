"use client";

import { useState } from "react";
import { ActionButton } from "@/components/action-button";
import type { components } from "@/lib/api-types";

type RemoteTrainingDispatchOut = components["schemas"]["RemoteTrainingDispatchOut"];

/**
 * POST /models/train-production -- remotely dispatches this repo's own train-production.yml
 * GitHub Actions workflow. Deliberately does NOT call POST /models/train: that endpoint runs
 * MODEL_TRAINING in-process on this same Render web service, which is exactly what OOM-crashed
 * the live API mid-training (see app.integrations.github_actions's own module docstring). This
 * button is the one-click counterpart to the text-only guidance GAMEWEEK_REVIEW's own retraining
 * push alert gives -- same workflow, triggered from the app itself instead of GitHub's own UI.
 */
export function RetrainButton() {
  const [result, setResult] = useState<RemoteTrainingDispatchOut | null>(null);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <ActionButton
        path="/models/train-production"
        label="Retrain model (production)"
        pendingLabel="Starting..."
        onSuccess={(data) => setResult(data as RemoteTrainingDispatchOut)}
      />
      {result && (
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--text-muted)" }}>
          Started -- this runs on GitHub Actions, not this app, and takes 30-60+ minutes.{" "}
          <a href={result.workflow_runs_url} target="_blank" rel="noreferrer">
            Watch progress on GitHub
          </a>
          .
        </p>
      )}
    </div>
  );
}
