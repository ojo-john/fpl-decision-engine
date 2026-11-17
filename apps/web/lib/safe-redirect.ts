/** Post-login redirect sanitizing, shared by the token login form and the GitHub sign-in routes. */

/**
 * Only same-origin relative paths are allowed as a post-login redirect target. Anything else
 * (absolute URLs, protocol-relative `//evil.com`, backslash tricks browsers normalize to `//`)
 * falls back to "/" -- otherwise the login flow becomes an open redirect.
 */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
  return next;
}
