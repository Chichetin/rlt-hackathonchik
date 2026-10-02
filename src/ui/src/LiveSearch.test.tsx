import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LIVE_IDLE, LIVE_STEPS, LiveSearch } from "./LiveSearch";

afterEach(() => vi.useRealTimers());

describe("LiveSearch (#583)", () => {
  it("shows the button and runs the search", () => {
    const run = vi.fn();
    render(<LiveSearch live={LIVE_IDLE} onRun={run} />);
    fireEvent.click(screen.getByRole("button", { name: /Искать в реальном времени/ }));
    expect(run).toHaveBeenCalledOnce();
  });

  it("while searching: the button stays disabled, the steps of the server one after another", () => {
    vi.useFakeTimers();
    render(<LiveSearch live={{ ...LIVE_IDLE, state: "loading" }} onRun={() => undefined} />);
    expect(screen.getByRole("button")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(LIVE_STEPS[0]);
    act(() => { vi.advanceTimersByTime(2700); });
    expect(screen.getByRole("status")).toHaveTextContent(LIVE_STEPS[1]);
  });

  it("the result is one short line, the funnel only under «Подробнее»", () => {
    render(<LiveSearch live={{ ...LIVE_IDLE, state: "done", text: "Найдено в реальном времени: 4", note: "Яндекс: запросов 3" }}
      onRun={() => undefined} />);
    expect(screen.getByRole("status")).toHaveTextContent("Найдено в реальном времени: 4");
    expect(screen.getByText("Подробнее")).toBeInTheDocument();
  });
});
