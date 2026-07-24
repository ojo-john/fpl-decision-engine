import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Card, ConfidenceBadge, EmptyState, ErrorState } from "@/components/ui";

describe("Card", () => {
  it("renders its children", () => {
    render(<Card>hello</Card>);
    expect(screen.getByText("hello")).toBeInTheDocument();
  });
});

describe("ConfidenceBadge", () => {
  it.each([
    ["low", "var(--danger)"],
    ["medium", "#e0a940"],
    ["high", "var(--success)"],
  ])("colors %s confidence with %s", (confidence, color) => {
    render(<ConfidenceBadge confidence={confidence} />);
    expect(screen.getByText(confidence)).toHaveStyle({ color });
  });

  it("is case-insensitive when matching a known confidence level", () => {
    render(<ConfidenceBadge confidence="HIGH" />);
    expect(screen.getByText("HIGH")).toHaveStyle({ color: "var(--success)" });
  });

  it("falls back to the muted color for an unrecognized confidence value", () => {
    render(<ConfidenceBadge confidence="unknown" />);
    expect(screen.getByText("unknown")).toHaveStyle({ color: "var(--text-muted)" });
  });
});

describe("EmptyState", () => {
  it("renders its message inside a Card", () => {
    render(<EmptyState>Nothing here yet.</EmptyState>);
    expect(screen.getByText("Nothing here yet.")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("renders its message in the danger color", () => {
    render(<ErrorState>Something broke.</ErrorState>);
    expect(screen.getByText("Something broke.")).toHaveStyle({ color: "var(--danger)" });
  });
});
