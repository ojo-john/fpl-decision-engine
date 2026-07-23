"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { buttonStyle } from "@/components/ui";

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
}: {
  path: string;
  body?: Record<string, unknown>;
  label: string;
  pendingLabel?: string;
  onSuccess?: (data: unknown) => void;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setPending(true);
    setError(null);
    try {
      const response = await fetch(`/api/proxy${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      const data: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        setError(extractErrorMessage(data) ?? `Request failed (${response.status}).`);
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
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <button onClick={handleClick} disabled={pending} style={{ ...buttonStyle, opacity: pending ? 0.7 : 1 }}>
        {pending ? (pendingLabel ?? "Working...") : label}
      </button>
      {error && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{error}</span>}
    </div>
  );
}

export function extractErrorMessage(data: unknown): string | null {
  if (data && typeof data === "object") {
    const record = data as Record<string, unknown>;
    if (typeof record.error_message === "string") return record.error_message;
    if (typeof record.detail === "string") return record.detail;
  }
  return null;
}
