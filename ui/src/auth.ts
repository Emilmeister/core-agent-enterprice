import Keycloak from "keycloak-js";
import type { Identity } from "./types";

export class LoginError extends Error {
  constructor(
    message: string,
    readonly switchAccount?: () => Promise<void>,
  ) {
    super(message);
  }
}

export class Session {
  valid = true;
  onExpired: () => void = () => {};
  constructor(
    readonly keycloak: Keycloak,
    readonly identity: Identity,
  ) {
    keycloak.onAuthLogout = () => this.expire();
    keycloak.onAuthRefreshError = () => this.expire();
  }
  expire() {
    if (!this.valid) return;
    this.valid = false;
    this.keycloak.clearToken();
    this.onExpired();
  }
  async token() {
    if (!this.valid) throw new Error("Сессия завершена. Войдите снова.");
    try {
      await this.keycloak.updateToken(30);
      if (!this.valid || !this.keycloak.token) throw new Error();
      return this.keycloak.token;
    } catch {
      this.expire();
      throw new Error("Сессия завершена. Войдите снова.");
    }
  }
  async logout() {
    const action = this.keycloak.logout({
      redirectUri: `${location.origin}/ui/?logged_out=1`,
    });
    this.expire();
    await action;
  }
}

export async function signIn(): Promise<Session> {
  const response = await fetch("/ui/config", {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok)
    throw new LoginError(
      "Вход в интерфейс не настроен. Обратитесь к администратору.",
    );
  const config: unknown = await response.json();
  if (
    !config ||
    typeof config !== "object" ||
    !("issuer" in config) ||
    !("client_id" in config) ||
    typeof config.issuer !== "string" ||
    typeof config.client_id !== "string"
  )
    throw new LoginError("Не удалось прочитать настройки входа.");
  const issuer = new URL(config.issuer);
  const marker = issuer.pathname.lastIndexOf("/realms/");
  if (marker < 0 || issuer.search || issuer.hash)
    throw new LoginError("Некорректные настройки входа.");
  const keycloak = new Keycloak({
    url: issuer.origin + issuer.pathname.slice(0, marker),
    realm: decodeURIComponent(issuer.pathname.slice(marker + 8)),
    clientId: config.client_id,
  });
  try {
    const authenticated = await keycloak.init({
      flow: "standard",
      pkceMethod: "S256",
      checkLoginIframe: false,
      enableLogging: false,
      redirectUri: `${location.origin}/ui/`,
    });
    if (!authenticated) await keycloak.login();
    await keycloak.updateToken(30);
    const identityResponse = await fetch("/api/identity", {
      headers: { Authorization: `Bearer ${keycloak.token}` },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    });
    if (!identityResponse.ok) {
      keycloak.clearToken();
      throw new LoginError(
        identityResponse.status === 403
          ? "Этот аккаунт не имеет доступа владельца. Войдите аккаунтом владельца."
          : "Не удалось проверить доступ. Попробуйте войти снова.",
        identityResponse.status === 403
          ? () => keycloak.logout({ redirectUri: `${location.origin}/ui/` })
          : undefined,
      );
    }
    const identity = (await identityResponse.json()) as Identity;
    if (
      identity.role !== "owner" ||
      typeof identity.actor_id !== "string" ||
      typeof identity.tenant !== "string"
    ) {
      keycloak.clearToken();
      throw new LoginError("Доступ владельца не подтверждён.");
    }
    return new Session(keycloak, identity);
  } catch (error) {
    keycloak.clearToken();
    throw error;
  }
}
