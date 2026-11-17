import { describe, expect, it } from "vitest";
import { safeNextPath } from "./safe-redirect";

describe("safeNextPath", () => {
  it.each([
    [null, "/"],
    [undefined, "/"],
    ["", "/"],
    ["https://evil.example.com", "/"],
    ["//evil.example.com", "/"],
    ["/\\evil.example.com", "/"],
    ["team", "/"],
    ["/team", "/team"],
    ["/players/12?tab=history", "/players/12?tab=history"],
  ])("safeNextPath(%s)_returns_%s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });
});
