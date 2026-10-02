import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { applyTheme, readTheme, ThemeToggle } from "./theme";

afterEach(() => { document.documentElement.removeAttribute("data-theme"); vi.restoreAllMocks(); });

test("toggle sets data-theme on html and remembers the choice", async () => {
  applyTheme(readTheme());
  render(<ThemeToggle />);
  expect(document.documentElement.dataset.theme).toBe("dark");
  await userEvent.click(screen.getByRole("button", { name: "Включить светлую тему" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(window.localStorage.getItem("mitrandir-theme")).toBe("light");
  expect(readTheme()).toBe("light");
  await userEvent.click(screen.getByRole("button", { name: "Включить тёмную тему" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(readTheme()).toBe("dark");
});

test("broken localStorage does not break the theme", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  expect(readTheme()).toBe("dark");
  applyTheme("dark");
  render(<ThemeToggle />);
  await userEvent.click(screen.getByRole("button"));
  expect(document.documentElement.dataset.theme).toBe("light");
});
