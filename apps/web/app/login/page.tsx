import { Suspense } from "react";
import { LoginForm } from "./login-form";

export default function LoginPage() {
  // Only offers "Sign in with GitHub" once it's configured. Deliberately a cheap presence check
  // (not readGithubOAuthConfig): a malformed allowlist must not take down the token login too --
  // it surfaces as an error from /api/auth/github itself instead.
  const githubEnabled = Boolean(process.env.GITHUB_OAUTH_CLIENT_ID);
  return (
    <main
      style={{
        minHeight: "100dvh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div style={{ width: "100%", maxWidth: "320px", padding: "1rem" }}>
        <h1 style={{ fontSize: "1.25rem", marginBottom: "1.5rem" }}>FPL Decision Engine</h1>
        <Suspense fallback={null}>
          <LoginForm githubEnabled={githubEnabled} />
        </Suspense>
      </div>
    </main>
  );
}
