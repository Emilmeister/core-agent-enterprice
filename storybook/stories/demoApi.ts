import Keycloak from "keycloak-js";
import { Api, ApiError } from "../../ui/src/api";
import { Session } from "../../ui/src/auth";
import { createDemoRequest } from "./demoData";
import type { DemoMode } from "./demoData";

export function createDemoApi(mode: DemoMode = "normal", state?: string) {
  // Never initialized: no login, refresh or token calls in this isolated app.
  const session = new Session(new Keycloak({ url: "https://auth.example.invalid", realm: "demo", clientId: "storybook" }),
    { actor_id: "demo-owner", role: "owner", tenant: "demo" });
  const api = new Api(session);
  const request = createDemoRequest(mode, state);
  api.request = async (path, options) => {
    const response = await request(path, options);
    if (!response.ok) throw new ApiError(response.status, (await response.json()).error.code);
    return response;
  };
  return api;
}
