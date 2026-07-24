import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// @testing-library/react does not auto-cleanup under Vitest (unlike Jest, where its
// setupFilesAfterEach hook is picked up automatically) -- without this, DOM from one test
// leaks into the next and later `getByRole` queries start matching multiple elements.
afterEach(() => {
  cleanup();
});
