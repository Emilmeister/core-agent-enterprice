import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { App } from "./App";
import { LoginError, signIn } from "./auth";
import type { Session } from "./auth";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);
function Entrance() {
  const signedOut = new URLSearchParams(location.search).get("logged_out") === "1";
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<LoginError | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  function connected(value: Session) {
    value.onExpired = () => {
      setSession(null);
      setPassword("");
      setError(new LoginError("Сессия завершена. Войдите снова.", undefined, value.authMode === "cloudru"));
    };
    setError(null);
    setSession(value);
  }
  async function login(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const entered = password;
    setPassword("");
    try {
      connected(await signIn(entered));
    } catch (failure) {
      setError(failure instanceof LoginError ? failure
        : new LoginError("Не удалось войти. Проверьте соединение и попробуйте снова.", undefined, true));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    if (signedOut) return;
    let live = true;
    void signIn()
      .then((value) => {
        if (live) connected(value);
        else value.expire();
      })
      .catch((failure) => {
        if (live)
          setError(
            failure instanceof LoginError
              ? failure
              : new LoginError(
                  "Не удалось войти. Проверьте соединение и попробуйте снова.",
                ),
          );
      });
    return () => {
      live = false;
    };
  }, [signedOut]);
  return session ? (
    <App session={session} />
  ) : (
    <main className="entrance">
      <div className="eyebrow">Рабочее пространство владельцев</div>
      <h1>{signedOut ? "Вы вышли из аккаунта" : error?.passwordRequired ? "Вход владельца" : error ? "Вход не завершён" : "Подключаем рабочее пространство"}</h1>
      <p id="login-status" role={error ? "alert" : "status"}>
        {signedOut ? "Чтобы продолжить работу, войдите снова." : error?.message || "Проверяем сессию и права доступа…"}
      </p>
      {!signedOut && error?.passwordRequired ? <form onSubmit={(event) => void login(event)}>
        <label>Пароль владельца<input type="password" autoComplete="current-password" autoFocus
          aria-describedby="login-status" value={password} onChange={(event) => setPassword(event.target.value)}
          required disabled={busy} /></label>
        <button disabled={busy || !password}>{busy ? "Входим…" : "Войти"}</button>
      </form> : (signedOut || error) && (
        <button
          onClick={() => {
            if (error?.switchAccount)
              void error
                .switchAccount()
                .catch(() =>
                  setError(
                    new LoginError(
                      "Не удалось завершить сессию. Повторите вход.",
                    ),
                  ),
                );
            else location.replace("/ui/");
          }}
        >
          {signedOut ? "Войти" : error?.switchAccount ? "Сменить аккаунт" : "Войти снова"}
        </button>
      )}
    </main>
  );
}
root.render(<Entrance />);
