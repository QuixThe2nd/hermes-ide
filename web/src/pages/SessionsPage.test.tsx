// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getSessions: vi.fn(),
  getStatus: vi.fn(),
  getEmptySessionsCount: vi.fn(),
  getSessionStats: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));
vi.mock("@/components/Markdown", () => ({ Markdown: () => null }));
vi.mock("@/components/PlatformsCard", () => ({ PlatformsCard: () => null }));
vi.mock("@/plugins", () => ({ PluginSlot: () => null }));
vi.mock("@/lib/dashboard-flags", () => ({
  isDashboardEmbeddedChatEnabled: () => false,
}));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setAfterTitle: vi.fn(), setEnd: vi.fn() }),
}));
vi.mock("@/contexts/useSystemActions", () => ({
  useSystemActions: () => ({
    activeAction: null,
    actionStatus: null,
    dismissLog: vi.fn(),
  }),
}));
vi.mock("@nous-research/ui/hooks/use-toast", () => ({
  useToast: () => ({ toast: null, showToast: vi.fn() }),
}));
/** A translation namespace: known keys render their label, unknown keys
 *  render empty instead of crashing the page under test. */
function namespace(labels: Record<string, string>): Record<string, string> {
  return new Proxy(labels, {
    get(target, key: string) {
      return key in target ? target[key] : "";
    },
  });
}

vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      common: namespace({
        msgs: "msgs",
        untitled: "Untitled",
        retry: "Retry",
        page: "Page",
        of: "of",
        clear: "Clear",
        live: "Live",
      }),
      sessions: namespace({
        overview: "Overview",
        history: "History",
        filterChats: "Chats",
        filterAutomation: "Automation",
        filterAll: "All",
        sourceFilter: "Session source",
        anySource: "Any source",
        searchPlaceholder: "Search message content...",
        previousPage: "Previous page",
        nextPage: "Next page",
        noSessions: "No sessions yet",
        selectSession: "Select session",
      }),
      status: namespace({
        gatewayFailedToStart: "Gateway failed to start",
        platformError: "error",
        platformDisconnected: "disconnected",
      }),
    },
  }),
}));

let container: HTMLDivElement;
let root: Root;

// React only routes updates through act() when this flag is set.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(ui));
  // The list load runs inside queueMicrotask; flush it so the page gets past
  // its full-page loading gate before assertions.
  await act(async () => {});
}

function text(): string {
  return container.textContent ?? "";
}

function button(label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll("button")).find((btn) =>
    (btn.textContent ?? "").includes(label),
  );
  if (!match) throw new Error(`no button labelled ${label}`);
  return match as HTMLButtonElement;
}

function iconButton(ariaLabel: string): HTMLButtonElement {
  const match = container.querySelector(`button[aria-label="${ariaLabel}"]`);
  if (!match) throw new Error(`no button aria-labelled ${ariaLabel}`);
  return match as HTMLButtonElement;
}

/** Labels of every Segmented option currently rendered. */
function radioLabels(): string[] {
  return Array.from(container.querySelectorAll('[role="radio"]')).map((el) =>
    (el.textContent ?? "").trim(),
  );
}

function sourceFilterButton(): HTMLButtonElement | null {
  return container.querySelector('button[aria-label="Session source"]');
}

/** Most recent getSessions call that was NOT the 24-hour window query. */
function lastListCall() {
  return [...apiMocks.getSessions.mock.calls]
    .reverse()
    .find(
      (call) =>
        (call[2] as { activeWithinHours?: number } | undefined)?.activeWithinHours ==
        null,
    );
}

/** Most recent getSessions call for the 24-hour window query. */
function lastOverviewCall() {
  return [...apiMocks.getSessions.mock.calls]
    .reverse()
    .find(
      (call) =>
        (call[2] as { activeWithinHours?: number } | undefined)?.activeWithinHours !=
        null,
    );
}

function sessionRow(id: string, title: string) {
  return {
    id,
    title,
    source: "cli",
    model: "nous/hermes-4",
    message_count: 12,
    last_active: Date.now() / 1000 - 60,
    started_at: Date.now() / 1000 - 120,
    is_active: false,
    preview: "preview text",
  };
}

/** Resolve the non-overview calls every mount makes. */
function stubAmbientCalls() {
  apiMocks.getStatus.mockResolvedValue({
    gateway_state: "running",
    gateway_platforms: {},
  });
  apiMocks.getEmptySessionsCount.mockResolvedValue({ count: 0 });
  apiMocks.getSessionStats.mockResolvedValue({
    total: 0,
    active_store: 0,
    archived: 0,
    messages: 0,
    by_source: {},
  });
}

beforeEach(() => {
  stubAmbientCalls();
  // History list: empty by default so the Overview is the interesting view.
  apiMocks.getSessions.mockImplementation(async (_limit, _offset, options) => {
    if (options && (options as { activeWithinHours?: number }).activeWithinHours) {
      return { sessions: [], total: 0, limit: 20, offset: 0 };
    }
    return { sessions: [], total: 0, limit: 20, offset: 0 };
  });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.clearAllMocks();
});

describe("SessionsPage overview (last 24 hours)", () => {
  it("is the default view and queries the server-side 24-hour window", async () => {
    await renderPage();

    expect(text()).toContain("Overview");

    const overviewCall = apiMocks.getSessions.mock.calls.find((call) => {
      const options = call[2] as { activeWithinHours?: number } | undefined;
      return options?.activeWithinHours != null;
    });
    expect(overviewCall).toBeDefined();
    expect(overviewCall![0]).toBe(20); // PAGE_SIZE, not an unbounded fetch
    expect(overviewCall![1]).toBe(0);
    expect(overviewCall![2]).toMatchObject({
      order: "recent",
      activeWithinHours: 24,
    });
  });

  it("renders the windowed rows with the compact overview row style and total", async () => {
    apiMocks.getSessions.mockImplementation(async (_limit, _offset, options) => {
      if (
        options &&
        (options as { activeWithinHours?: number }).activeWithinHours != null
      ) {
        return {
          sessions: [
            sessionRow("s1", "Windowed conversation"),
            sessionRow("s2", "Another one"),
          ],
          total: 2,
          limit: 20,
          offset: 0,
        };
      }
      return { sessions: [], total: 99, limit: 20, offset: 0 };
    });

    await renderPage();

    const page = text();
    expect(page).toContain("Active in the last 24 hours");
    expect(page).toContain("Windowed conversation");
    expect(page).toContain("Another one");
    // Compact row facts: model short name, message count, relative activity,
    // source label.
    expect(page).toContain("hermes-4");
    expect(page).toContain("12 msgs");
    expect(page).toContain("CLI");
    // Matching total for the window (the History list's 99 must not leak in).
    expect(page).toContain("2");
    expect(page).not.toContain("Page 1 of");
  });

  it("shows a loading state before the first window resolves, without claiming empty", async () => {
    let resolveOverview: (value: unknown) => void = () => {};
    const deferred = new Promise((resolve) => {
      resolveOverview = resolve;
    });
    apiMocks.getSessions.mockImplementation(async (_limit, _offset, options) => {
      if (
        options &&
        (options as { activeWithinHours?: number }).activeWithinHours != null
      ) {
        return deferred.then(() => ({
          sessions: [sessionRow("s1", "Late arrival")],
          total: 1,
          limit: 20,
          offset: 0,
        }));
      }
      return { sessions: [], total: 0, limit: 20, offset: 0 };
    });

    await renderPage();

    // Card header renders immediately; the body neither lists rows nor
    // declares the window empty nor errors while the request is in flight.
    expect(text()).toContain("Active in the last 24 hours");
    expect(text()).not.toContain("Late arrival");
    expect(text()).not.toContain("No sessions were active");
    expect(text()).not.toContain("Failed to load sessions");

    await act(async () => {
      resolveOverview(undefined);
    });

    expect(text()).toContain("Late arrival");
  });

  it("states specifically that no sessions were active in the last 24 hours", async () => {
    await renderPage();

    expect(text()).toContain("No sessions were active in the last 24 hours");
    // The window total is 0, and the empty overview stays on the Overview
    // view rather than collapsing into the History list.
    expect(text()).toContain("Active in the last 24 hours");
    expect(text()).not.toContain("No sessions yet");
  });

  it("surfaces a retryable error when the window request fails", async () => {
    let fail = true;
    apiMocks.getSessions.mockImplementation(async (_limit, _offset, options) => {
      if (
        options &&
        (options as { activeWithinHours?: number }).activeWithinHours != null
      ) {
        if (fail) throw new Error("backend down");
        return {
          sessions: [sessionRow("s1", "Recovered")],
          total: 1,
          limit: 20,
          offset: 0,
        };
      }
      return { sessions: [], total: 0, limit: 20, offset: 0 };
    });

    await renderPage();

    expect(text()).toContain("Failed to load sessions from the last 24 hours");
    expect(text()).toContain("backend down");
    // Empty-state text must not mask the failure.
    expect(text()).not.toContain("No sessions were active");

    fail = false;
    await act(async () => {
      button("Retry").click();
    });

    expect(text()).toContain("Recovered");
    expect(text()).not.toContain("Failed to load sessions");
  });

  it("paginates the window with the server total across pages", async () => {
    apiMocks.getSessions.mockImplementation(
      async (limit: number, offset: number, options) => {
        if (
          options &&
          (options as { activeWithinHours?: number }).activeWithinHours != null
        ) {
          const page = Math.floor(offset / limit);
          return {
            sessions: [
              sessionRow(`s-${page}-0`, `Row ${page}-0`),
              sessionRow(`s-${page}-1`, `Row ${page}-1`),
            ],
            total: 45,
            limit,
            offset,
          };
        }
        return { sessions: [], total: 0, limit: 20, offset: 0 };
      },
    );

    await renderPage();

    expect(text()).toContain("Page 1 of 3");
    expect(text()).toContain("Row 0-0");

    await act(async () => {
      iconButton("Next page").click();
    });

    expect(text()).toContain("Page 2 of 3");
    expect(text()).toContain("Row 1-0");
    expect(text()).not.toContain("Row 0-0");
    const pagedCall = apiMocks.getSessions.mock.calls
      .filter((call) => (call[2] as { activeWithinHours?: number })?.activeWithinHours)
      .pop();
    expect(pagedCall![0]).toBe(20);
    expect(pagedCall![1]).toBe(20);
  });

  it("keeps the full History list on its own tab behind the same toggle", async () => {
    apiMocks.getSessions.mockImplementation(async (_limit, _offset, options) => {
      if (
        options &&
        (options as { activeWithinHours?: number }).activeWithinHours != null
      ) {
        return { sessions: [], total: 0, limit: 20, offset: 0 };
      }
      return {
        sessions: [sessionRow("old-1", "Ancient conversation")],
        total: 87,
        limit: 20,
        offset: 0,
      };
    });

    await renderPage();

    expect(text()).not.toContain("Ancient conversation");

    await act(async () => {
      button("History").click();
    });

    // The History manager still lists everything, unwindowed.
    expect(text()).toContain("Ancient conversation");
    expect(text()).not.toContain("Active in the last 24 hours");
  });

  it("hides the category and source filters on the Overview; History still shows and applies them", async () => {
    // Two known sources so the source dropdown has real options to toggle.
    apiMocks.getSessionStats.mockResolvedValue({
      total: 0,
      active_store: 0,
      archived: 0,
      messages: 0,
      by_source: { cli: 2, cron: 1 },
    });

    await renderPage();

    // Overview: the only segmented control is the view toggle. The window
    // spans all sources, so the category radios and the source dropdown —
    // filters it never applies — must not render.
    expect(radioLabels()).toEqual(["Overview", "History"]);
    expect(sourceFilterButton()).toBeNull();
    // ...and the window query itself carries no category/source scoping.
    expect(lastOverviewCall()![2]).toEqual({
      order: "recent",
      activeWithinHours: 24,
    });

    await act(async () => {
      button("History").click();
    });

    // History: both filters are back...
    expect(radioLabels()).toEqual([
      "Chats",
      "Automation",
      "All",
      "Overview",
      "History",
    ]);
    expect(sourceFilterButton()).not.toBeNull();

    // ...and the category filter still scopes the list query (cron is the
    // only automation source, so cli is excluded).
    await act(async () => {
      button("Automation").click();
    });
    expect(lastListCall()![2]).toMatchObject({ excludeSources: ["cli"] });

    // The source dropdown still scopes the list query too: under "All",
    // deselecting cron leaves cli as the single selected source.
    await act(async () => {
      button("All").click();
    });
    await act(async () => {
      sourceFilterButton()!.click();
    });
    await act(async () => {
      button("Cron").click();
    });
    expect(lastListCall()![2]).toMatchObject({ source: "cli" });

    // Switching back to the Overview hides the filters again without ever
    // rescoping the window query.
    await act(async () => {
      button("Overview").click();
    });
    expect(radioLabels()).toEqual(["Overview", "History"]);
    expect(sourceFilterButton()).toBeNull();
    expect(lastOverviewCall()![2]).toEqual({
      order: "recent",
      activeWithinHours: 24,
    });
  });

  it("reloads to the last valid page when the window shrinks under the reader", async () => {
    vi.useFakeTimers();
    try {
      // Server-side window total the overview query reports. Dropping it
      // mid-session is the rolling-window reality: rows age out of the last
      // 24 hours and the page the user is ON stops existing.
      let total = 45;
      apiMocks.getSessions.mockImplementation(
        async (limit: number, offset: number, options) => {
          if (
            options &&
            (options as { activeWithinHours?: number }).activeWithinHours != null
          ) {
            const page = Math.floor(offset / limit);
            if (page >= Math.ceil(total / limit)) {
              // The clamped server view: the requested page is past the end.
              return { sessions: [], total, limit, offset };
            }
            return {
              sessions: [sessionRow(`s-${page}`, `Row ${page}`)],
              total,
              limit,
              offset,
            };
          }
          return { sessions: [], total: 0, limit: 20, offset: 0 };
        },
      );

      await renderPage();
      expect(text()).toContain("Page 1 of 3");

      await act(async () => {
        iconButton("Next page").click();
      });
      expect(text()).toContain("Page 2 of 3");
      expect(text()).toContain("Row 1");

      // The window shrinks to 15 rows: page 2 no longer exists while page 1
      // still has results. The 5s poll delivers the now-empty page.
      total = 15;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });

      // Clamped back onto the last valid page and reloaded: page 1's rows
      // render and pagination reflects the shrunken window...
      expect(text()).toContain("Row 0");
      expect(text()).not.toContain("Row 1");
      expect(text()).not.toContain("Page 2 of");
      const clampedCall = apiMocks.getSessions.mock.calls
        .filter((call) => (call[2] as { activeWithinHours?: number })?.activeWithinHours)
        .pop();
      expect(clampedCall![1]).toBe(0);
      // ...and the card never claimed the window was empty on the way there.
      expect(text()).not.toContain("No sessions were active");
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders the Overview while the History request is still pending", async () => {
    // History never resolves — a hung backend — while the window query
    // answers immediately. The full-page spinner belongs to the History
    // view, so the DEFAULT view must still show its own rows.
    apiMocks.getSessions.mockImplementation(async (_limit, _offset, options) => {
      if (
        options &&
        (options as { activeWithinHours?: number }).activeWithinHours != null
      ) {
        return {
          sessions: [sessionRow("s1", "Resolved overview row")],
          total: 1,
          limit: 20,
          offset: 0,
        };
      }
      return new Promise(() => {});
    });

    await renderPage();

    expect(text()).toContain("Active in the last 24 hours");
    expect(text()).toContain("Resolved overview row");
    // The History tab's own loading/empty states must not bleed in.
    expect(text()).not.toContain("No sessions were active");
    expect(text()).not.toContain("No sessions yet");
  });
});

/** Lazy import so the module-level vi.mock calls are installed first. */
async function renderPage() {
  const { default: SessionsPage } = await import("./SessionsPage");
  await render(
    <MemoryRouter>
      <SessionsPage />
    </MemoryRouter>,
  );
}
