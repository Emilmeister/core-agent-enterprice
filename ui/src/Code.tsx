import { useMemo } from "react";
import type { ReactNode } from "react";
import { createLowlight } from "lowlight";
import python from "highlight.js/lib/languages/python";

const highlighter = createLowlight({ python });
type Token = ReturnType<typeof highlighter.highlight>["children"][number];

function token(node: Token, index: number): ReactNode {
  if (node.type === "text") return node.value;
  if (node.type !== "element") return null;
  const classes = node.properties.className;
  // Render only spans and React text; highlighted source never becomes raw HTML.
  return <span key={index} className={Array.isArray(classes) ? classes.join(" ") : undefined}>
    {node.children.map(token)}
  </span>;
}

export function HighlightedCode({ text, language }: { text: string; language?: string }) {
  const isPython = ["python", "py", "python3"].includes(language?.toLowerCase() ?? "");
  const content = useMemo(() => {
    if (!isPython || text.length > 20_000) return text;
    try { return highlighter.highlight("python", text).children.map(token); }
    catch { return text; }
  }, [text, isPython]);
  return <code className={isPython ? "language-python" : undefined}>{content}</code>;
}

export function CodeBlock({ text, language }: { text: string; language?: string }) {
  return <pre className="code-block" tabIndex={0}>
    <HighlightedCode text={text} language={language} />
  </pre>;
}
