import { expect, test } from "vitest";
import { deepLinkSearch, parseDeepLink } from "./deeplink";

test("parse: lot, text, nothing, junk", () => {
  expect(parseDeepLink("?lot=5903085")).toEqual({ lot: "5903085" });
  expect(parseDeepLink("?lot=%205903085%20")).toEqual({ lot: "5903085" });
  expect(parseDeepLink("?q=%D0%B1%D1%83%D0%BC%D0%B0%D0%B3%D0%B0")).toEqual({ q: "бумага" });
  expect(parseDeepLink("?lot=5&q=x")).toEqual({ lot: "5" });
  expect(parseDeepLink("?lot=abc&q=x")).toEqual({ q: "x" });
  expect(parseDeepLink("?lot=abc")).toEqual({});
  expect(parseDeepLink("?q=%20%20")).toEqual({});
  expect(parseDeepLink("")).toEqual({});
  expect(parseDeepLink(`?q=${"а".repeat(900)}`).q).toHaveLength(500);
});

test("build: lot link for a loaded lot, text link otherwise, round trip", () => {
  expect(deepLinkSearch("5903085", true, "бумага")).toBe("?lot=5903085");
  expect(deepLinkSearch("5903085", false, "бумага А4")).toBe(`?q=${encodeURIComponent("бумага").replace(/%20/g, "+")}+${encodeURIComponent("А4")}`);
  expect(deepLinkSearch("12x", true, "кабель")).toBe("?q=%D0%BA%D0%B0%D0%B1%D0%B5%D0%BB%D1%8C");
  expect(deepLinkSearch("", false, "  ")).toBe("");
  expect(parseDeepLink(deepLinkSearch("", false, "бумага А4"))).toEqual({ q: "бумага А4" });
  expect(parseDeepLink(deepLinkSearch("77", true, "x"))).toEqual({ lot: "77" });
});
