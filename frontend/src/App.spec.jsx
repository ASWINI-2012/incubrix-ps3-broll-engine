// Runtime validation coverage for: IncuBrix Studio navigation and dashboard rendering.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import App from "./App.jsx";

describe("IncuBrix Studio", () => {
  it("renders the workspace navigation and dashboard entry point", () => {
    const markup = renderToStaticMarkup(createElement(App));

    expect(markup).toContain("IncuBrix");
    expect(markup).toContain("Dashboard");
    expect(markup).toContain("Create Video");
    expect(markup).toContain("Make something");
    expect(markup).toContain("Create new video");
  });
});
