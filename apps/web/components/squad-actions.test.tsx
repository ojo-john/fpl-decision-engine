import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SquadActions } from "@/components/squad-actions";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

// H2HSimulatePicker fetches its own rival list over the network on mount -- irrelevant to
// SquadActions' own logic (which player-count/raw-picks conditionals it renders), and would
// otherwise need a full browserClient() mock just to keep this test from erroring on fetch.
vi.mock("@/components/h2h-simulate-picker", () => ({
  H2HSimulatePicker: () => <div data-testid="h2h-picker" />,
}));

const FULL_SQUAD = Array.from({ length: 15 }, (_, i) => `player-${i}`);

describe("SquadActions", () => {
  it("offers lineup optimization for a full 15-player squad", () => {
    render(<SquadActions snapshotId="snap-1" squadPlayerIds={FULL_SQUAD} hasRawPicks={true} />);
    expect(screen.getByRole("button", { name: "Optimize lineup" })).toBeInTheDocument();
  });

  it("explains why lineup optimization is unavailable for an incomplete squad, instead of offering it", () => {
    render(<SquadActions snapshotId="snap-1" squadPlayerIds={FULL_SQUAD.slice(0, 14)} hasRawPicks={true} />);
    expect(screen.queryByRole("button", { name: "Optimize lineup" })).not.toBeInTheDocument();
    expect(
      screen.getByText(/Lineup optimization needs all 15 squad players resolved/),
    ).toBeInTheDocument();
  });

  it("always offers transfer optimization and chip simulation regardless of squad size", () => {
    render(<SquadActions snapshotId="snap-1" squadPlayerIds={FULL_SQUAD.slice(0, 3)} hasRawPicks={true} />);
    expect(screen.getByRole("button", { name: "Optimize transfers" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Simulate chips" })).toBeInTheDocument();
  });

  it("shows the solver-fallback note when the squad has no confirmed starting XI", () => {
    render(<SquadActions snapshotId="snap-1" squadPlayerIds={FULL_SQUAD} hasRawPicks={false} />);
    expect(
      screen.getByText(/no confirmed starting XI, so chip\/H2H simulation will estimate one/),
    ).toBeInTheDocument();
  });

  it("omits the solver-fallback note when the squad already has a confirmed starting XI", () => {
    render(<SquadActions snapshotId="snap-1" squadPlayerIds={FULL_SQUAD} hasRawPicks={true} />);
    expect(screen.queryByText(/no confirmed starting XI/)).not.toBeInTheDocument();
  });

  it("always renders the H2H simulate picker", () => {
    render(<SquadActions snapshotId="snap-1" squadPlayerIds={FULL_SQUAD} hasRawPicks={true} />);
    expect(screen.getByTestId("h2h-picker")).toBeInTheDocument();
  });
});
