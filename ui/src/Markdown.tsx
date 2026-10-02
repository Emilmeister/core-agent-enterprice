import { useEffect, useId, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

function Diagram({ source }: { source: string }) {
  const id = "diagram-" + useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [image, setImage] = useState<{ source: string; url: string }>();
  const [failed, setFailed] = useState<string>();
  useEffect(() => {
    let stopped = false;
    let url: string | undefined;
    void (async () => {
      try {
        if (source.length > 20_000) throw new Error("Diagram too large");
        const { default: mermaid } = await import("mermaid");
        if (stopped) return;
        mermaid.initialize({
          startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true,
          maxTextSize: 20_000, maxEdges: 500, htmlLabels: false,
          secure: ["securityLevel", "startOnLoad", "maxTextSize", "maxEdges", "htmlLabels", "secure"],
        });
        const { svg } = await mermaid.render(id, source);
        if (stopped) return;
        // Mermaid's HTML serialization can omit namespaces required by SVG images.
        const template = document.createElement("template");
        template.innerHTML = svg;
        const diagram = template.content.querySelector("svg");
        if (!diagram) throw new Error("Missing diagram");
        // SVG in image context cannot execute scripts, navigate or access the page.
        url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(diagram)], { type: "image/svg+xml" }));
        setImage({ source, url });
      } catch {
        if (!stopped) setFailed(source);
      }
    })();
    return () => {
      stopped = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [id, source]);
  const ready = image?.source === source;
  return <figure className="diagram">
    {ready && <img src={image.url} alt="Диаграмма Mermaid" />}
    {!ready && <p role="status">{failed === source
      ? "Не удалось построить диаграмму. Исходный код доступен ниже."
      : "Строим диаграмму…"}</p>}
    <details open={!ready}>
      <summary>Исходный код диаграммы</summary>
      <pre><code>{source}</code></pre>
    </details>
  </figure>;
}

// Stable component types preserve Diagram state during chat polling/timer renders.
const components: Components = {
  img: ({ alt }) => <span>{alt ? `[Изображение: ${alt}]` : "[Изображение]"}</span>,
  a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
  pre: ({ children }) => <div className="code-block">{children}</div>,
  code: ({ className, children }) => className === "language-mermaid"
    ? <Diagram source={String(children).replace(/\n$/, "")} />
    : <code className={className}>{children}</code>,
};

export function Markdown({ text }: { text: string }) {
  return <div className="markdown">
    <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={components}>{text}</ReactMarkdown>
  </div>;
}
