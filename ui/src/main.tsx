import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { App } from "./App";
import { LoginError, signIn } from "./auth";
import type { Session } from "./auth";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);
function Entrance() {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<LoginError | null>(null);
  useEffect(() => {
    let live = true;
    void signIn()
      .then((value) => {
        if (live) {
          value.onExpired = () => {
            setSession(null);
            setError(new LoginError("Сессия завершена. Войдите снова."));
          };
          setSession(value);
        } else value.expire();
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
  }, []);
  return session ? (
    <App session={session} />
  ) : (
    <main className="entrance">
      <div className="brand">
        <span className="brand-mark">c</span>core / agent
      </div>
      <div className="eyebrow">Рабочее пространство владельцев</div>
      <h1>{error ? "Вход не завершён" : "Подключаем рабочее пространство"}</h1>
      <p role={error ? "alert" : "status"}>
        {error?.message || "Проверяем сессию и права доступа…"}
      </p>
      {error && (
        <button
          onClick={() => {
            if (error.switchAccount)
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
          {error.switchAccount ? "Сменить аккаунт" : "Войти снова"}
        </button>
      )}
    </main>
  );
}
root.render(<Entrance />);
