"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { buttonStyle, inputStyle } from "@/components/ui";
import { safeNextPath } from "@/lib/safe-redirect";

// Shown when /api/auth/github/callback redirects back with ?error=<code> (lib/github-oauth.ts).
const GITHUB_ERROR_MESSAGES: Record<string, string> = {
  github_not_configured: "GitHub sign-in isn't configured on this deployment.",
  github_state_mismatch: "GitHub sign-in expired or was interrupted. Try again.",
  github_denied: "GitHub sign-in was cancelled or denied.",
  github_unavailable: "Couldn't reach GitHub. Try again, or use the access token.",
  github_not_allowed: "That GitHub account isn't allowed to access this app.",
};

export function LoginForm({ githubEnabled }: { githubEnabled: boolean }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = safeNextPath(searchParams.get("next"));
  const githubErrorCode = searchParams.get("error");
  const githubError = githubErrorCode ? (GITHUB_ERROR_MESSAGES[githubErrorCode] ?? null) : null;

  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(githubError);
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
      {githubEnabled && (
        <>
          <a
            href={`/api/auth/github?next=${encodeURIComponent(next)}`}
            style={{
              ...buttonStyle,
              fontSize: "1rem",
              padding: "0.6rem 0.75rem",
              textAlign: "center",
              textDecoration: "none",
            }}
          >
            Sign in with GitHub
          </a>
          <p style={{ margin: 0, textAlign: "center", fontSize: "0.85rem", color: "var(--text-muted)" }}>
            or use the access token
          </p>
        </>
      )}
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
