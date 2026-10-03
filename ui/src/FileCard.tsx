import { useLayoutEffect, useRef, useState } from "react";
import { Api, errorText } from "./api";
import { formatFileSize } from "./types";
import { Markdown } from "./Markdown";
import { CodeBlock } from "./Code";
import type { AttachmentEntry, HistoryItem, ResponseFileEntry } from "./types";

const previewLimit = 64 * 1024;
const textTypes: Record<string, string> = {
  txt: "Текст", md: "Markdown", markdown: "Markdown", csv: "CSV", json: "JSON", yaml: "YAML", yml: "YAML",
  log: "Журнал", py: "Python", js: "JavaScript", ts: "TypeScript", tsx: "TypeScript",
  jsx: "JavaScript", css: "CSS", sh: "Shell", ini: "Конфигурация", toml: "TOML", xml: "XML",
};
const otherTypes: Record<string, string> = {
  pdf: "PDF", png: "Изображение PNG", jpg: "Изображение JPEG", jpeg: "Изображение JPEG",
  gif: "Изображение GIF", webp: "Изображение WebP", svg: "Изображение SVG", html: "HTML",
  htm: "HTML", doc: "Документ Word", docx: "Документ Word", xls: "Таблица Excel",
  xlsx: "Таблица Excel", ppt: "Презентация", pptx: "Презентация", zip: "Архив ZIP",
};

export function FileCard({ api, contextId, taskId, file }: {
  api?: Api; contextId?: string; taskId?: string; file: AttachmentEntry | ResponseFileEntry;
}) {
  const responseFile = "file_id" in file;
  const name = responseFile ? file.name : file.actual_name;
  const identity = responseFile ? file.file_id : file.relative_path;
  const extension = name.split(".").at(-1)?.toLowerCase() ?? "";
  const textPreview = Object.hasOwn(textTypes, extension)
    || (responseFile && file.media_type.startsWith("text/") && !["html", "htm", "svg"].includes(extension)
      && !["text/html", "image/svg+xml"].includes(file.media_type.split(";")[0]));
  const fileType = textTypes[extension] ?? otherTypes[extension] ?? "Файл";
  const [busy, setBusy] = useState<"open" | "download" | null>(null);
  const [error, setError] = useState("");
  const [opened, setOpened] = useState(false);
  const [preview, setPreview] = useState<{ text: string; truncated: boolean } | null>(null);
  const pending = useRef<AbortController | undefined>(undefined);
  const urls = useRef(new Set<string>());
  useLayoutEffect(() => {
    setBusy(null);
    setError("");
    setOpened(false);
    setPreview(null);
    pending.current = undefined;
    return () => {
      pending.current?.abort();
      for (const url of urls.current) URL.revokeObjectURL(url);
      urls.current.clear();
    };
  }, [api, contextId, taskId, identity]);

  async function load(mode: "open" | "download") {
    if (mode === "open") {
      setOpened(true);
      if (!textPreview || preview) return;
    }
    if (!api || !contextId || pending.current || !api.session.valid || (responseFile && !taskId)) return;
    const abort = new AbortController();
    pending.current = abort;
    const stopped = () => abort.signal.aborted || pending.current !== abort || !api.session.valid;
    setBusy(mode);
    setError("");
    try {
      const path = responseFile
        ? `/api/chats/${encodeURIComponent(contextId)}/tasks/${encodeURIComponent(taskId!)}/files/${encodeURIComponent(file.file_id)}`
        : `/api/chats/${encodeURIComponent(contextId)}/files/content?${new URLSearchParams({ path: file.relative_path })}`;
      const response = await api.request(path, { signal: abort.signal });
      if (mode === "open") {
        if (!response.body) throw new Error("Missing file body");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let bytes = 0, content = "", truncated = false;
        try {
          while (!stopped()) {
            const chunk = await reader.read();
            if (chunk.done) break;
            const remaining = previewLimit - bytes;
            content += decoder.decode(chunk.value.subarray(0, remaining), { stream: true });
            bytes += Math.min(chunk.value.length, remaining);
            if (chunk.value.length > remaining || bytes === previewLimit) {
              truncated = chunk.value.length > remaining || file.size_bytes > previewLimit;
              break;
            }
          }
          content += decoder.decode();
          if (!stopped()) setPreview({ text: content, truncated });
        } finally {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      } else {
        const blob = await response.blob();
        if (stopped()) return;
        const url = URL.createObjectURL(blob);
        urls.current.add(url);
        const link = document.createElement("a");
        link.href = url;
        link.download = name.replace(/[\\/\u0000-\u001f\u007f]/g, "_") || "файл";
        link.click();
        window.setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url); }, 1000);
      }
    } catch (failure) {
      if (!stopped()) setError(errorText(failure));
    } finally {
      if (!stopped()) setBusy(null);
      if (pending.current === abort) pending.current = undefined;
    }
  }

  const unavailable = !api || !contextId || !api.session.valid || (responseFile && !taskId);
  return <li className="file-card">
    <div className="file-card-heading"><strong>{name}</strong><span className="muted">{fileType} · {formatFileSize(file.size_bytes)}</span></div>
    <div className="file-card-actions">
      <button type="button" className="secondary" disabled={!!busy || unavailable}
        aria-label={`Открыть ${name}`} onClick={() => void load("open")}>{busy === "open" ? "Открываем…" : "Открыть"}</button>
      <button type="button" className="secondary" disabled={!!busy || unavailable}
        aria-label={`Скачать ${name}`} onClick={() => void load("download")}>{busy === "download" ? "Скачиваем…" : "Скачать"}</button>
    </div>
    {opened && <div className="file-preview">
      <div className="file-preview-heading"><strong>Предпросмотр</strong><button type="button" className="text-button"
        aria-label={`Закрыть предпросмотр ${name}`} onClick={() => { pending.current?.abort(); pending.current = undefined; setBusy(null); setOpened(false); }}>Закрыть</button></div>
      <div className="file-preview-content" tabIndex={0} role="region" aria-label={`Предпросмотр ${name}`}>
      {!textPreview && <p className="muted">Предпросмотр этого формата недоступен. Скачайте файл, чтобы открыть его.</p>}
      {textPreview && busy === "open" && <p className="muted" role="status">Загружаем текст…</p>}
      {preview && <>{["md", "markdown"].includes(extension)
        ? <Markdown text={preview.text || "Пустой файл"} />
        : extension === "py" ? <CodeBlock text={preview.text || "Пустой файл"} language="python" />
        : <pre>{preview.text || "Пустой файл"}</pre>
        }{preview.truncated && <p className="muted">Показаны первые {formatFileSize(previewLimit)}. Полный файл доступен для скачивания.</p>}</>}
      </div>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
    <details className="file-properties"><summary>Свойства файла</summary><dl>
      {!responseFile && <><dt>Путь</dt><dd>{file.relative_path}</dd></>}
      {responseFile && <><dt>ID</dt><dd>{file.file_id}</dd></>}
      <dt>Размер в байтах</dt><dd>{file.size_bytes}</dd><dt>SHA-256</dt><dd>{file.sha256}</dd>
    </dl></details>
  </li>;
}

export function ConversationFiles({ api, contextId, items, onJump }: {
  api: Api; contextId: string; items: HistoryItem[]; onJump: (id: string) => void;
}) {
  const attached = items.filter((item) => item.status === "available" && item.attachments?.length);
  const created = items.filter((item) => item.kind === "result" && item.status === "available"
    && item.outcome?.state === "COMPLETED" && item.task_id && item.response_files?.length);
  return <div className="conversation-files">
    {([{ title: "Прикреплённые", rows: attached, output: false }, { title: "Созданные агентом", rows: created, output: true }] as const).map(({ title, rows, output }) => {
      const count = rows.reduce((total, item) => total + (output ? item.response_files?.length ?? 0 : item.attachments?.length ?? 0), 0);
      return <section key={title} aria-label={title}>
        <h3>{title} <span className="muted">{count}</span></h3>
        {!count && <p className="muted">В загруженной истории файлов нет.</p>}
        {rows.map((item) => <div className="conversation-file-source" key={item.id}>
          <ul className={`message-attachments ${output ? "response-files" : ""}`}>
            {(output ? item.response_files! : item.attachments!).map((file) => <FileCard key={"file_id" in file ? file.file_id : file.index}
              api={api} contextId={contextId} taskId={item.task_id ?? undefined} file={file} />)}
          </ul>
          <button type="button" className="text-button" onClick={() => onJump(item.id)}>К исходному сообщению</button>
        </div>)}
      </section>;
    })}
  </div>;
}
