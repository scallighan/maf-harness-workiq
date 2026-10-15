import {
  InteractionRequiredAuthError,
  PublicClientApplication,
  type AccountInfo,
} from "@azure/msal-browser";
import { z } from "zod";
import { SessionSchema, type PublicConfig, type Session } from "./models";

type InitialCatalog = { sessions: Session[]; created: Session | null } | null;

export class SessionBootstrap {
  private readonly requests = new Map<string, Promise<InitialCatalog>>();

  load(identity: string, token: () => Promise<string>, isCurrent: () => boolean): Promise<InitialCatalog> {
    const pending = this.requests.get(identity);
    if (pending) return pending;
    const request = this.start(token, isCurrent).catch((failure: unknown) => {
      if (this.requests.get(identity) === request) this.requests.delete(identity);
      throw failure;
    });
    this.requests.set(identity, request);
    return request;
  }

  clear() {
    this.requests.clear();
  }

  private async start(token: () => Promise<string>, isCurrent: () => boolean): Promise<InitialCatalog> {
    const accessToken = await token();
    if (!isCurrent()) return null;
    const sessions = await api("/api/sessions", z.array(SessionSchema), accessToken);
    if (!isCurrent()) return null;
    if (sessions.length > 0) return { sessions, created: null };
    const created = await api("/api/sessions", SessionSchema, accessToken, { method: "POST", body: "{}" });
    return isCurrent() ? { sessions: [created], created } : null;
  }
}

export class SignIn {
  readonly client: PublicClientApplication;
  private readonly tokenRequests = new Map<string, Promise<string>>();

  constructor(readonly config: PublicConfig) {
    this.client = new PublicClientApplication({
      auth: {
        clientId: config.clientId,
        authority: `https://login.microsoftonline.com/${config.tenantId}`,
        redirectUri: `${window.location.origin}/auth.html`,
        postLogoutRedirectUri: `${window.location.origin}/auth.html`,
      },
      cache: { cacheLocation: "sessionStorage" },
    });
  }

  async initialize(): Promise<AccountInfo | null> {
    await this.client.initialize();
    const account = this.client.getActiveAccount();
    return account?.tenantId === this.config.tenantId ? account : null;
  }

  async login(retry = false): Promise<AccountInfo> {
    const result = await this.client.loginPopup({
      scopes: [this.config.scope],
      prompt: "select_account",
      overrideInteractionInProgress: retry,
    });
    this.client.setActiveAccount(result.account);
    return result.account;
  }

  token(account: AccountInfo): Promise<string> {
    const pending = this.tokenRequests.get(account.homeAccountId);
    if (pending) return pending;
    const request = this.acquireToken(account).finally(() => {
      if (this.tokenRequests.get(account.homeAccountId) === request) {
        this.tokenRequests.delete(account.homeAccountId);
      }
    });
    this.tokenRequests.set(account.homeAccountId, request);
    return request;
  }

  private async acquireToken(account: AccountInfo): Promise<string> {
    const request = { account, scopes: [this.config.scope] };
    try {
      return (await this.client.acquireTokenSilent(request)).accessToken;
    } catch (error) {
      if (!(error instanceof InteractionRequiredAuthError)) throw error;
      return (await this.client.acquireTokenPopup(request)).accessToken;
    }
  }

  async logout(account: AccountInfo): Promise<void> {
    await this.client.logoutPopup({ account, mainWindowRedirectUri: `${window.location.origin}/` });
  }
}

export async function checkedResponse(response: Response): Promise<Response> {
  if (response.ok) return response;
  const fallback = `Request failed (HTTP ${response.status}).`;
  if (!response.headers.get("content-type")?.includes("application/json")) throw new Error(fallback);
  const value: unknown = await response.json().catch(() => {
    throw new Error(fallback);
  });
  const detail = z.object({ detail: z.string() }).safeParse(value);
  throw new Error(detail.success ? detail.data.detail : fallback);
}

export async function api<S extends z.ZodTypeAny>(
  path: string, schema: S, token: string, init: RequestInit = {},
): Promise<z.output<S>> {
  const response = await checkedResponse(await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
      Authorization: `Bearer ${token}`,
    },
    cache: "no-store",
  }));
  return schema.parse(await response.json());
}
