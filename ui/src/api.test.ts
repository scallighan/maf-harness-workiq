import { afterEach, describe, expect, it, vi } from "vitest";
import { InteractionRequiredAuthError, type AccountInfo } from "@azure/msal-browser";
import { SessionBootstrap, SignIn, checkedResponse } from "./api";

const mocks = vi.hoisted(() => ({
  configuration: vi.fn(),
  initialize: vi.fn().mockResolvedValue(undefined),
  loginPopup: vi.fn(),
  acquireTokenSilent: vi.fn(),
  acquireTokenPopup: vi.fn(),
  setActiveAccount: vi.fn(),
  logoutPopup: vi.fn(),
}));

vi.mock("@azure/msal-browser", async (original) => {
  const actual = await original<typeof import("@azure/msal-browser")>();
  return {
    ...actual,
    PublicClientApplication: class {
      constructor(configuration: unknown) { mocks.configuration(configuration); }
      initialize = mocks.initialize;
      loginPopup = mocks.loginPopup;
      acquireTokenSilent = mocks.acquireTokenSilent;
      acquireTokenPopup = mocks.acquireTokenPopup;
      setActiveAccount = mocks.setActiveAccount;
      logoutPopup = mocks.logoutPopup;
    },
  };
});

const account: AccountInfo = {
  homeAccountId: "user.tenant", localAccountId: "user", tenantId: "tenant",
  username: "user", environment: "login.microsoftonline.com",
};
const config = { tenantId: "tenant", clientId: "browser", scope: "api://api/access_as_user", agentName: "agent" };

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function signIn() {
  vi.stubGlobal("window", { location: { origin: "https://ui.example.com" } });
  return new SignIn(config);
}

describe("popup authentication", () => {
  it("returns sign-in and sign-out to the dedicated same-origin redirect bridge", async () => {
    const login = signIn();
    expect(mocks.configuration.mock.calls[0][0].auth).toMatchObject({
      redirectUri: "https://ui.example.com/auth.html",
      postLogoutRedirectUri: "https://ui.example.com/auth.html",
    });
    mocks.loginPopup.mockResolvedValue({ account });
    await login.login();
    expect(mocks.loginPopup).toHaveBeenLastCalledWith(expect.objectContaining({
      overrideInteractionInProgress: false,
    }));
    await login.login(true);
    expect(mocks.loginPopup).toHaveBeenLastCalledWith(expect.objectContaining({
      overrideInteractionInProgress: true,
    }));
  });

  it("coalesces concurrent token requests instead of starting competing consent popups", async () => {
    const login = signIn();
    mocks.acquireTokenSilent.mockRejectedValue(new InteractionRequiredAuthError(
      "interaction_required", "Interactive consent is required.",
    ));
    mocks.acquireTokenPopup.mockResolvedValue({ accessToken: "delegated-test-token" });
    const values = await Promise.all([login.token(account), login.token(account)]);
    expect(values).toEqual(["delegated-test-token", "delegated-test-token"]);
    expect(mocks.acquireTokenSilent).toHaveBeenCalledTimes(1);
    expect(mocks.acquireTokenPopup).toHaveBeenCalledTimes(1);
    await login.token(account);
    expect(mocks.acquireTokenSilent).toHaveBeenCalledTimes(2);
  });
});

describe("gateway errors", () => {
  it("surfaces an HTTP error for HTML or malformed JSON without turning it into success", async () => {
    await expect(checkedResponse(new Response("<html>Unavailable</html>", {
      status: 503, headers: { "Content-Type": "text/html" },
    }))).rejects.toThrow("HTTP 503");
    await expect(checkedResponse(new Response("{", {
      status: 502, headers: { "Content-Type": "application/json" },
    }))).rejects.toThrow("HTTP 502");
  });

  describe("first-session bootstrap", () => {
    const session = {
      id: "11111111-1111-4111-8111-111111111111", title: "New conversation",
      created_at: "2026-10-14T12:00:00Z", updated_at: "2026-10-14T12:00:00Z", running: false,
    };
    const response = (value: unknown) => new Response(JSON.stringify(value), {
      headers: { "Content-Type": "application/json" },
    });

    it("creates one session for an empty catalog and coalesces repeated initialization", async () => {
      const fetch = vi.fn().mockResolvedValueOnce(response([])).mockResolvedValueOnce(response(session));
      vi.stubGlobal("fetch", fetch);
      const bootstrap = new SessionBootstrap();
      const first = bootstrap.load("user:1", async () => "delegated-token", () => true);
      const second = bootstrap.load("user:1", async () => "delegated-token", () => true);
      expect(first).toBe(second);
      expect(await first).toEqual({ sessions: [session], created: session });
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(fetch.mock.calls[1][1].method).toBe("POST");
    });

    it("does not create another session when the user already has one", async () => {
      const fetch = vi.fn().mockResolvedValue(response([session]));
      vi.stubGlobal("fetch", fetch);
      const value = await new SessionBootstrap().load("user:1", async () => "delegated-token", () => true);
      expect(value).toEqual({ sessions: [session], created: null });
      expect(fetch).toHaveBeenCalledTimes(1);
    });

    it("does not create or select a session if the account changes while the catalog loads", async () => {
      let current = true;
      const fetch = vi.fn().mockImplementation(async () => {
        current = false;
        return response([]);
      });
      vi.stubGlobal("fetch", fetch);
      const value = await new SessionBootstrap().load("user:1", async () => "delegated-token", () => current);
      expect(value).toBeNull();
      expect(fetch).toHaveBeenCalledTimes(1);
    });

    it("surfaces a failed creation instead of inventing a usable session", async () => {
      const fetch = vi.fn().mockResolvedValueOnce(response([])).mockResolvedValueOnce(new Response(
        '{"detail":"Session storage unavailable."}', { status: 503, headers: { "Content-Type": "application/json" } },
      ));
      vi.stubGlobal("fetch", fetch);
      await expect(new SessionBootstrap().load("user:1", async () => "delegated-token", () => true))
        .rejects.toThrow("Session storage unavailable.");
    });
  });
});
