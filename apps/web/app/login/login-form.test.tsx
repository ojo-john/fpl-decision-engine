import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { LoginForm } from "./login-form";

let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => searchParams,
}));

describe("LoginForm", () => {
  it("githubNotAllowedError_showsReadableMessage", () => {
    searchParams = new URLSearchParams("error=github_not_allowed");
    render(<LoginForm githubEnabled />);
    expect(screen.getByText("That GitHub account isn't allowed to access this app.")).toBeTruthy();
  });

  it("unknownErrorCode_showsNoError", () => {
    searchParams = new URLSearchParams("error=<script>");
    render(<LoginForm githubEnabled />);
    expect(screen.queryByText(/script|isn't|couldn't|cancelled|expired/i)).toBeNull();
  });

  it("githubDisabled_hidesGithubButton", () => {
    searchParams = new URLSearchParams();
    render(<LoginForm githubEnabled={false} />);
    expect(screen.queryByText("Sign in with GitHub")).toBeNull();
  });

  it("githubEnabled_buttonCarriesNextPath", () => {
    searchParams = new URLSearchParams("next=/team");
    render(<LoginForm githubEnabled />);
    expect(screen.getByText("Sign in with GitHub").getAttribute("href")).toBe("/api/auth/github?next=%2Fteam");
  });

  it("absoluteNextUrl_buttonFallsBackToRoot", () => {
    searchParams = new URLSearchParams("next=https://evil.example.com");
    render(<LoginForm githubEnabled />);
    expect(screen.getByText("Sign in with GitHub").getAttribute("href")).toBe("/api/auth/github?next=%2F");
  });
});
