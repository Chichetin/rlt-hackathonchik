import "@testing-library/jest-dom/vitest";
import { beforeEach } from "vitest";

// the app keeps the page in the URL and the last query in sessionStorage: every test starts on a clean "/"
beforeEach(() => {
  window.history.replaceState(null, "", "/");
  window.sessionStorage.clear();
  window.localStorage.clear(); // local scoring weights (#179)
});

// jsdom has no media queries; behavior tests use the accessible, instant-motion preference.
Object.defineProperty(window, "matchMedia", { writable: true, value: (query: string) => ({
  matches: query.includes("prefers-reduced-motion"), media: query, onchange: null,
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
}) });

// jsdom has no layout engine. Scroll behavior itself is exercised in Playwright.
class VisibleIntersectionObserver implements IntersectionObserver {
  readonly root = null;
  readonly rootMargin = "0px";
  readonly thresholds = [0];
  constructor(private callback: IntersectionObserverCallback) {}
  observe(target: Element) {
    const rect = target.getBoundingClientRect();
    this.callback([{ target, isIntersecting: true, intersectionRatio: 1, time: 0,
      boundingClientRect: rect, intersectionRect: rect, rootBounds: null }], this);
  }
  unobserve() {}
  disconnect() {}
  takeRecords() { return []; }
}
Object.defineProperty(globalThis, "IntersectionObserver", { writable: true, value: VisibleIntersectionObserver });
