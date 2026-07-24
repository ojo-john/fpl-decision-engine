import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ManualSquadForm } from "./manual-squad-form";
import type { components } from "@/lib/api-types";

type PlayerSummary = components["schemas"]["PlayerSummary"];

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush, refresh: vi.fn() }),
}));

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("@/lib/browser-client", () => ({
  browserClient: () => ({ GET: getMock, POST: postMock }),
}));

function makePlayer(index: number, overrides: Partial<PlayerSummary> = {}): PlayerSummary {
  return {
    id: `player-${index}`,
    fpl_element_id: 30000 + index,
    web_name: `Player${index}`,
    team_id: "team-1",
    team_short_name: "ARS",
    position: "MID",
    now_cost: 50 + index,
    status: "a",
    total_points: 0,
    form: null,
    points_per_game: null,
    ep_next: null,
    is_quarantined: false,
    ...overrides,
  };
}

async function search(items: PlayerSummary[]) {
  getMock.mockResolvedValueOnce({ data: { items, page: 1, page_size: 50, total: items.length }, response: { status: 200 } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(screen.queryByText("Searching...")).not.toBeInTheDocument());
}

describe("ManualSquadForm", () => {
  beforeEach(() => {
    routerPush.mockClear();
    getMock.mockReset();
    postMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts with an empty 0/15 selection when no initialSquad is given", () => {
    render(<ManualSquadForm />);
    expect(screen.getByText("Selected squad (0/15) -- total 0.0")).toBeInTheDocument();
  });

  it("prefills the selection, bank, and free transfers from initialSquad", () => {
    const players = [makePlayer(1, { now_cost: 50 }), makePlayer(2, { now_cost: 60 })];
    render(
      <ManualSquadForm initialSquad={{ players, bank: 15, freeTransfersAvailable: 2 }} />,
    );
    expect(screen.getByText("Selected squad (2/15) -- total 11.0")).toBeInTheDocument();
    expect(screen.getByLabelText("Bank")).toHaveValue(15);
    expect(screen.getByLabelText("Free transfers")).toHaveValue(2);
  });

  it("adds a searched player to the selection and sums cost correctly", async () => {
    render(<ManualSquadForm />);
    await search([makePlayer(1, { now_cost: 55 }), makePlayer(2, { now_cost: 45 })]);

    const addButtons = screen.getAllByRole("button", { name: "Add" });
    expect(addButtons).toHaveLength(2);
    fireEvent.click(addButtons[0] as HTMLElement);

    expect(screen.getByText("Selected squad (1/15) -- total 5.5")).toBeInTheDocument();
  });

  it("disables the Add button (and shows Added) once a player is already selected", async () => {
    render(<ManualSquadForm />);
    await search([makePlayer(1)]);

    const addButton = screen.getByRole("button", { name: "Add" });
    fireEvent.click(addButton);

    expect(await screen.findByRole("button", { name: "Added" })).toBeDisabled();
  });

  it("never lets the selection exceed 15 players", async () => {
    render(<ManualSquadForm />);
    const sixteenPlayers = Array.from({ length: 16 }, (_, i) => makePlayer(i, { now_cost: 40 }));
    await search(sixteenPlayers);

    for (const button of screen.getAllByRole("button", { name: "Add" })) {
      fireEvent.click(button);
    }

    expect(screen.getByText("Selected squad (15/15) -- total 60.0")).toBeInTheDocument();
    // The 16th player's own Add button must be disabled, not just ignored -- addPlayer's own
    // size >= SQUAD_SIZE guard is a belt-and-braces backstop for a click that shouldn't be
    // reachable in the first place.
    const remainingAddButtons = screen.getAllByRole("button", { name: "Add" });
    for (const button of remainingAddButtons) {
      expect(button).toBeDisabled();
    }
  });

  it("removes a selected player and reduces the total cost", async () => {
    render(<ManualSquadForm />);
    await search([makePlayer(1, { now_cost: 55, web_name: "Removeme" })]);
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText("Selected squad (1/15) -- total 5.5")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    expect(screen.getByText("Selected squad (0/15) -- total 0.0")).toBeInTheDocument();
  });

  it("only enables Create squad once exactly 15 players are selected, and navigates on success", async () => {
    const players = Array.from({ length: 15 }, (_, i) => makePlayer(i, { now_cost: 40 }));
    render(<ManualSquadForm initialSquad={{ players, bank: 0, freeTransfersAvailable: 1 }} />);
    postMock.mockResolvedValueOnce({
      data: { manager_squad_snapshot_id: "new-snap-id" },
      error: undefined,
      response: { status: 201 },
    });

    const submit = screen.getByRole("button", { name: "Create squad" });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/team/new-snap-id"));
    expect(postMock).toHaveBeenCalledWith(
      "/squads/manual",
      expect.objectContaining({
        body: expect.objectContaining({
          fpl_element_ids: players.map((p) => p.fpl_element_id),
        }),
      }),
    );
  });
});
