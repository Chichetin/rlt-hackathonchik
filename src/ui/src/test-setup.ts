import "@testing-library/jest-dom/vitest";
import { beforeEach } from "vitest";

// the app keeps the page in the URL and the last query in sessionStorage: every test starts on a clean "/"
beforeEach(() => {
  window.history.replaceState(null, "", "/");
  window.sessionStorage.clear();
});
