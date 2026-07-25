"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { buttonStyle } from "@/components/ui";

/** Solver-backed actions (squad build/preview) measured live at 10-30
 * seconds with no server-side progress to report (execute_job is a single
 * synchronous call, nothing to poll) -- ticks once a second while a request
 * is in flight so a stalled-looking button reads as "still working", not
 * hung. Deliberately started/stopped from the click handler itself, not a
 * useEffect keyed on `pending`: an effect-based reset would either violate
 * react-hooks/set-state-in-effect (a synchronous setState at the top of the
 * effect body, not inside a subscription callback) or, if the reset were
 * dropped to satisfy that rule, leave a second click resuming the counter
 * from the previous run's stale elapsed value instead of restarting at 0. */
export function useElapsedTimer(): { elapsedSeconds: number; start: () => void; stop: () => void } {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const intervalId = useRef<ReturnType<typeof setInterval> | null>(null);

  function start() {
    setElapsedSeconds(0);
    const startedAt = Date.now();
    intervalId.current = setInterval(() => {
      setElapsedSeconds(Math.round((Date.now() - startedAt) / 1000));
    }, 1000);
  }

  function stop() {
    if (intervalId.current !== null) {
      clearInterval(intervalId.current);
      intervalId.current = null;
    }
    setElapsedSeconds(0);
  }

  return { elapsedSeconds, start, stop };
}

export const STILL_WORKING_THRESHOLD_SECONDS = 10;
export const STILL_WORKING_COPY = "Still working -- a full squad solve can take up to 30 seconds.";

export function pendingButtonLabel(pendingLabel: string, elapsed: number): string {
  return `${pendingLabel} ${elapsed}s`;
}

/**
 * A single no-input action (e.g. "generate a recommendation") -- POSTs
 * through /api/proxy (see that route's own docstring for why: the browser
 * never holds the real backend token). On success, either navigates
 * wherever `onSuccess` (given the parsed response body) says to, or -- if
 * `onSuccess` is omitted -- just refreshes the current Server Component
 * page so newly-created data shows up immediately.
 */
export function ActionButton({
  path,
  body,
  label,
  pendingLabel,
  onSuccess,
  mapError,
}: {
  path: string;
  body?: Record<string, unknown>;
  label: string;
  pendingLabel?: string;
  onSuccess?: (data: unknown) => void;
  /** Checked before the generic extractErrorMessage fallback -- lets a
   * specific action recognize a known error_code (e.g. the optimization
   * gate refusing pre-season) and explain it in context, rather than
   * surfacing the raw backend error string. Return null to fall through. */
  mapError?: (data: unknown) => string | null;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { elapsedSeconds, start, stop } = useElapsedTimer();

  async function handleClick() {
    setPending(true);
    setError(null);
    start();
    try {
      const response = await fetch(`/api/proxy${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      const data: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        setError(mapError?.(data) ?? extractErrorMessage(data) ?? `Request failed (${response.status}).`);
        return;
      }
      if (onSuccess) {
        onSuccess(data);
      } else {
        router.refresh();
      }
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
      stop();
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <button onClick={handleClick} disabled={pending} style={{ ...buttonStyle, opacity: pending ? 0.7 : 1 }}>
        {pending ? pendingButtonLabel(pendingLabel ?? "Working...", elapsedSeconds) : label}
      </button>
      {pending && elapsedSeconds >= STILL_WORKING_THRESHOLD_SECONDS && (
        <span style={{ color: "var(--text-muted)", fontSize: "0.8rem" }}>{STILL_WORKING_COPY}</span>
      )}
      {error && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{error}</span>}
    </div>
  );
}

/** error_code values app.optimization.gate.check_optimization_gate returns
 * when it refuses SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION/TRANSFER_OPTIMIZATION/
 * CHIP_SIMULATION pre-season (no gameweek marked is_current yet). Shared by
 * every action that can hit this gate, so each one explains it consistently
 * rather than surfacing the raw backend code. */
export const OPTIMIZATION_GATE_ERROR_CODES = new Set([
  "OPTIMIZATION_NOT_PERMITTED",
  "NO_INGESTION_DATA",
  "RULES_BLOCKED",
]);

export function isOptimizationGateError(data: unknown): boolean {
  const record = data as { error_code?: string } | null;
  return !!record?.error_code && OPTIMIZATION_GATE_ERROR_CODES.has(record.error_code);
}

export function extractErrorMessage(data: unknown): string | null {
  if (data && typeof data === "object") {
    const record = data as Record<string, unknown>;
    if (typeof record.error_message === "string") return record.error_message;
    if (typeof record.detail === "string") return record.detail;
    // FastAPI's own 422 validation-error shape: detail is a list of
    // {loc: ["body", "field_name"], msg: "...", type: "..."} objects, not a
    // string -- flatten it into one readable line instead of falling
    // through to a generic "(422)" message.
    if (Array.isArray(record.detail)) {
      const messages = record.detail
        .map((entry) => extractValidationErrorMessage(entry))
        .filter((message): message is string => message !== null);
      if (messages.length > 0) return messages.join("; ");
    }
  }
  return null;
}

function extractValidationErrorMessage(entry: unknown): string | null {
  if (!entry || typeof entry !== "object") return null;
  const record = entry as Record<string, unknown>;
  if (typeof record.msg !== "string") return null;
  const loc = Array.isArray(record.loc) ? record.loc : [];
  // Drop the leading "body"/"query"/"path" location segment -- meaningful to
  // the API, not to a user reading a form-validation message.
  const field = loc.filter((segment) => segment !== "body" && segment !== "query").join(".");
  return field ? `${field}: ${record.msg}` : record.msg;
}
