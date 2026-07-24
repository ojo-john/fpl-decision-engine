import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ActionButton, extractErrorMessage } from "@/components/action-button";

const routerRefresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: routerRefresh }),
}));

describe("extractErrorMessage", () => {
  it("prefers error_message over detail when both are present", () => {
    expect(extractErrorMessage({ error_message: "job failed", detail: "ignored" })).toBe("job failed");
  });

  it("returns a plain string detail as-is", () => {
    expect(extractErrorMessage({ detail: "Not found." })).toBe("Not found.");
  });

  it("flattens FastAPI's array-shaped 422 detail into readable field: message lines", () => {
    const data = {
      detail: [
        { loc: ["body", "fpl_manager_id"], msg: "Field required", type: "missing" },
        { loc: ["body", "bank"], msg: "Input should be a valid integer", type: "int_type" },
      ],
    };
    expect(extractErrorMessage(data)).toBe(
      "fpl_manager_id: Field required; bank: Input should be a valid integer",
    );
  });

  it("drops the leading body/query location segment but keeps nested field paths", () => {
    const data = { detail: [{ loc: ["body", "squad", "0", "id"], msg: "invalid" }] };
    expect(extractErrorMessage(data)).toBe("squad.0.id: invalid");
  });

  it("falls back to the bare message when loc is empty or missing", () => {
    expect(extractErrorMessage({ detail: [{ msg: "Something went wrong" }] })).toBe(
      "Something went wrong",
    );
  });

  it("skips malformed entries in the detail array without crashing", () => {
    const data = { detail: [{ loc: ["body", "a"] }, { loc: ["body", "b"], msg: "ok" }, "not-an-object"] };
    expect(extractErrorMessage(data)).toBe("b: ok");
  });

  it("returns null for an entirely empty array", () => {
    expect(extractErrorMessage({ detail: [] })).toBeNull();
  });

  it("returns null for null, non-object, or unrecognized shapes", () => {
    expect(extractErrorMessage(null)).toBeNull();
    expect(extractErrorMessage("just a string")).toBeNull();
    expect(extractErrorMessage({ unrelated_field: "x" })).toBeNull();
  });
});

describe("ActionButton", () => {
  beforeEach(() => {
    routerRefresh.mockClear();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts to /api/proxy<path> with the given body and refreshes on success when no onSuccess is given", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );

    render(<ActionButton path="/optimize/transfers" body={{ manager_squad_snapshot_id: "abc" }} label="Go" />);
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => expect(routerRefresh).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/proxy/optimize/transfers",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ manager_squad_snapshot_id: "abc" }),
      }),
    );
  });

  it("calls onSuccess with the parsed body instead of refreshing when provided", async () => {
    const onSuccess = vi.fn();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ transfer_optimization_run_id: "run-1" }), { status: 200 }),
    );

    render(<ActionButton path="/optimize/transfers" label="Go" onSuccess={onSuccess} />);
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ transfer_optimization_run_id: "run-1" }));
    expect(routerRefresh).not.toHaveBeenCalled();
  });

  it("shows the pending label while the request is in flight", async () => {
    let resolveFetch: (value: Response) => void = () => {};
    vi.mocked(fetch).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );

    render(<ActionButton path="/x" label="Go" pendingLabel="Working hard..." />);
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByRole("button", { name: "Working hard..." })).toBeDisabled();
    resolveFetch(new Response(JSON.stringify({}), { status: 200 }));
  });

  it("renders the extracted error message and does not refresh on a failed response", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ error_message: "Optimization gate refused." }), { status: 409 }),
    );

    render(<ActionButton path="/optimize/transfers" label="Go" />);
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText("Optimization gate refused.")).toBeInTheDocument();
    expect(routerRefresh).not.toHaveBeenCalled();
  });

  it("shows a generic network error when fetch itself throws", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("network down"));

    render(<ActionButton path="/x" label="Go" />);
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText("Could not reach the server.")).toBeInTheDocument();
  });
});
