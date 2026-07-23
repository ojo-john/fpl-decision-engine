"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { buttonStyle, inputStyle } from "@/components/ui";

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = searchParams.get("next") ?? "/";

  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!response.ok) {
        setError("Incorrect token.");
        return;
      }
      router.push(next);
      router.refresh();
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <label htmlFor="token" style={{ fontSize: "0.9rem", color: "var(--text-muted)" }}>
        Access token
      </label>
      <input
        id="token"
        type="password"
        autoComplete="current-password"
        value={token}
        onChange={(event) => setToken(event.target.value)}
        autoFocus
        style={{ ...inputStyle, fontSize: "1rem", padding: "0.6rem 0.75rem" }}
      />
      {error && <p style={{ color: "var(--danger)", margin: 0 }}>{error}</p>}
      <button
        type="submit"
        disabled={submitting || token.length === 0}
        style={{
          ...buttonStyle,
          fontSize: "1rem",
          padding: "0.6rem 0.75rem",
          cursor: submitting ? "wait" : "pointer",
          opacity: token.length === 0 ? 0.6 : 1,
        }}
      >
        {submitting ? "Signing in..." : "Sign in"}
      </button>
    </form>
  );
}
