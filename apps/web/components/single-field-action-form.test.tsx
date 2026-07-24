import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SingleFieldActionForm } from "@/components/single-field-action-form";

const routerRefresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: routerRefresh }),
}));

describe("SingleFieldActionForm", () => {
  beforeEach(() => {
    routerRefresh.mockClear();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("submits a number field coerced to a Number, merged with extraBody", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }));
    const user = userEvent.setup();

    render(
      <SingleFieldActionForm
        path="/optimize/transfers"
        fieldName="horizon_gameweeks"
        fieldLabel="Horizon"
        fieldType="number"
        submitLabel="Optimize"
        extraBody={{ manager_squad_snapshot_id: "snap-1" }}
      />,
    );
    await user.type(screen.getByRole("spinbutton"), "6");
    fireEvent.click(screen.getByRole("button", { name: "Optimize" }));

    await waitFor(() => expect(routerRefresh).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/proxy/optimize/transfers",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ manager_squad_snapshot_id: "snap-1", horizon_gameweeks: 6 }),
      }),
    );
  });

  it("clears the field after a successful submit", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }));
    const user = userEvent.setup();

    render(
      <SingleFieldActionForm
        path="/jobs/review-gameweek"
        fieldName="target_fpl_event_id"
        fieldLabel="Gameweek"
        fieldType="number"
        submitLabel="Run review"
      />,
    );
    const input = screen.getByRole("spinbutton") as HTMLInputElement;
    await user.type(input, "12");
    fireEvent.click(screen.getByRole("button", { name: "Run review" }));

    await waitFor(() => expect(input.value).toBe(""));
  });

  it("shows the extracted error and keeps the typed value on a failed submit", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "No gameweek found." }), { status: 404 }),
    );
    const user = userEvent.setup();

    render(
      <SingleFieldActionForm
        path="/jobs/review-gameweek"
        fieldName="target_fpl_event_id"
        fieldLabel="Gameweek"
        fieldType="number"
        submitLabel="Run review"
      />,
    );
    const input = screen.getByRole("spinbutton") as HTMLInputElement;
    await user.type(input, "999");
    fireEvent.click(screen.getByRole("button", { name: "Run review" }));

    expect(await screen.findByText("No gameweek found.")).toBeInTheDocument();
    expect(input.value).toBe("999");
    expect(routerRefresh).not.toHaveBeenCalled();
  });
});
