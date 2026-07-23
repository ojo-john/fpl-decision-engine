"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { buttonStyle, inputStyle } from "@/components/ui";
import { extractErrorMessage } from "@/components/action-button";

/** Same /api/proxy + router.refresh() pattern as ActionButton, for the one-field-input
 * actions (sync a league by id, review a specific gameweek by event number). */
export function SingleFieldActionForm({
  path,
  fieldName,
  fieldLabel,
  fieldType = "text",
  submitLabel,
  extraBody,
}: {
  path: string;
  fieldName: string;
  fieldLabel: string;
  fieldType?: "text" | "number";
  submitLabel: string;
  extraBody?: Record<string, unknown>;
}) {
  const router = useRouter();
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const body = {
        ...extraBody,
        [fieldName]: fieldType === "number" ? Number(value) : value,
      };
      const response = await fetch(`/api/proxy${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const data: unknown = await response.json().catch(() => null);
        setError(extractErrorMessage(data) ?? `Request failed (${response.status}).`);
        return;
      }
      setValue("");
      router.refresh();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
      <label style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>{fieldLabel}</label>
      <input
        type={fieldType}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        required
        style={{ ...inputStyle, width: "120px" }}
      />
      <button type="submit" disabled={pending} style={{ ...buttonStyle, opacity: pending ? 0.7 : 1 }}>
        {pending ? "Working..." : submitLabel}
      </button>
      {error && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{error}</span>}
    </form>
  );
}
