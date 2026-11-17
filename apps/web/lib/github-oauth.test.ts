import { describe, expect, it, vi } from "vitest";
import {
  buildAuthorizeUrl,
  fetchGithubUserId,
  type FetchFn,
  type GithubOAuthConfig,
  GithubSignInError,
  parseAllowedUserIds,
  parseStateCookie,
  readGithubOAuthConfig,
  serializeStateCookie,
  verifyGithubSignIn,
} from "./github-oauth";

const CONFIG: GithubOAuthConfig = {
  clientId: "client-id",
  clientSecret: "client-secret",
  allowedUserIds: new Set([62852503]),
};
const REDIRECT_URI = "https://app.example.com/api/auth/github/callback";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A fetch that answers the token exchange and /user calls with the given responses, in order. */
function sequenceFetch(...responses: Array<Response | Error>): FetchFn {
  const queue = [...responses];
  return vi.fn(async () => {
    const next = queue.shift();
    if (!next) throw new Error("unexpected extra fetch call");
    if (next instanceof Error) throw next;
    return next;
  }) as unknown as FetchFn;
}

async function signInErrorCode(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof GithubSignInError) return error.code;
    throw error;
  }
  throw new Error("expected a GithubSignInError");
}

describe("verifyGithubSignIn", () => {
  it("tokenExchangeNetworkError_throwsUnavailable", async () => {
    const fetchFn = sequenceFetch(new TypeError("fetch failed"));
    expect(await signInErrorCode(verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI))).toBe(
      "github_unavailable",
    );
  });

  it("tokenExchangeTimeout_throwsUnavailable", async () => {
    const fetchFn = sequenceFetch(new DOMException("timed out", "TimeoutError"));
    expect(await signInErrorCode(verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI))).toBe(
      "github_unavailable",
    );
  });

  it("tokenExchange5xx_throwsUnavailable", async () => {
    const fetchFn = sequenceFetch(new Response("oops", { status: 502 }));
    expect(await signInErrorCode(verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI))).toBe(
      "github_unavailable",
    );
  });

  it("badVerificationCode_throwsDenied", async () => {
    const fetchFn = sequenceFetch(jsonResponse({ error: "bad_verification_code" }));
    expect(await signInErrorCode(verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI))).toBe(
      "github_denied",
    );
  });

  it("malformedTokenBody_throwsUnavailable", async () => {
    const fetchFn = sequenceFetch(new Response("not json", { status: 200 }));
    expect(await signInErrorCode(verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI))).toBe(
      "github_unavailable",
    );
  });

  it("userNotAllowlisted_throwsNotAllowed", async () => {
    const fetchFn = sequenceFetch(jsonResponse({ access_token: "gho_x" }), jsonResponse({ id: 1 }));
    expect(await signInErrorCode(verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI))).toBe(
      "github_not_allowed",
    );
  });

  it("allowlistedUser_returnsUserId", async () => {
    const fetchFn = sequenceFetch(jsonResponse({ access_token: "gho_x" }), jsonResponse({ id: 62852503 }));
    expect(await verifyGithubSignIn(fetchFn, CONFIG, "code", REDIRECT_URI)).toBe(62852503);
  });
});

describe("fetchGithubUserId", () => {
  it("rejectedToken_throwsDenied", async () => {
    const fetchFn = sequenceFetch(new Response("", { status: 401 }));
    expect(await signInErrorCode(fetchGithubUserId(fetchFn, "gho_x"))).toBe("github_denied");
  });

  it("stringId_throwsUnavailable", async () => {
    const fetchFn = sequenceFetch(jsonResponse({ id: "62852503" }));
    expect(await signInErrorCode(fetchGithubUserId(fetchFn, "gho_x"))).toBe("github_unavailable");
  });

  it("validToken_sendsBearerHeader", async () => {
    const fetchFn = sequenceFetch(jsonResponse({ id: 5 }));
    await fetchGithubUserId(fetchFn, "gho_x");
    const init = (fetchFn as unknown as ReturnType<typeof vi.fn>).mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer gho_x");
  });
});

describe("parseAllowedUserIds", () => {
  it("emptyString_returnsEmptySet", () => {
    expect(parseAllowedUserIds("").size).toBe(0);
  });

  it("nonNumericEntry_throws", () => {
    expect(() => parseAllowedUserIds("123,ojo-john")).toThrow(/non-numeric/);
  });

  it("commaSeparatedWithSpaces_parsesEveryId", () => {
    expect([...parseAllowedUserIds(" 1, 2 ,,3")]).toEqual([1, 2, 3]);
  });
});

describe("readGithubOAuthConfig", () => {
  it("missingClientSecret_returnsNull", () => {
    expect(readGithubOAuthConfig({ GITHUB_OAUTH_CLIENT_ID: "id", GITHUB_AUTH_ALLOWED_USER_IDS: "1" })).toBeNull();
  });

  it("emptyAllowlist_returnsNull", () => {
    expect(
      readGithubOAuthConfig({ GITHUB_OAUTH_CLIENT_ID: "id", GITHUB_OAUTH_CLIENT_SECRET: "s" }),
    ).toBeNull();
  });

  it("fullyConfigured_returnsConfig", () => {
    const config = readGithubOAuthConfig({
      GITHUB_OAUTH_CLIENT_ID: "id",
      GITHUB_OAUTH_CLIENT_SECRET: "s",
      GITHUB_AUTH_ALLOWED_USER_IDS: "7",
    });
    expect(config?.allowedUserIds.has(7)).toBe(true);
  });
});

describe("state cookie", () => {
  it("tamperedValue_parsesAsNull", () => {
    expect(parseStateCookie("{not json")).toBeNull();
  });

  it("wrongShape_parsesAsNull", () => {
    expect(parseStateCookie(JSON.stringify({ state: 1, next: "/" }))).toBeNull();
  });

  it("roundTrip_preservesValues", () => {
    const value = { state: "abc", next: "/team?x=1" };
    expect(parseStateCookie(serializeStateCookie(value))).toEqual(value);
  });
});

describe("buildAuthorizeUrl", () => {
  it("includesClientStateAndRedirect_withNoScopes", () => {
    const url = new URL(buildAuthorizeUrl("client-id", REDIRECT_URI, "state-123"));
    expect([
      url.origin + url.pathname,
      url.searchParams.get("client_id"),
      url.searchParams.get("state"),
      url.searchParams.get("redirect_uri"),
      url.searchParams.has("scope"),
    ]).toEqual(["https://github.com/login/oauth/authorize", "client-id", "state-123", REDIRECT_URI, false]);
  });
});
