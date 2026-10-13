import { FileCard } from "./FileCard";
import type { Api } from "./api";
import type { ResponseArtifact } from "./types";

const labels = { text: "Текст", data: "Структурированные данные · JSON" };

export function ArtifactCards({ api, contextId, taskId, artifacts }: {
  api: Api; contextId: string; taskId: string; artifacts: ResponseArtifact[];
}) {
  return <div className="response-artifacts" aria-label="Опубликованные материалы">
    {artifacts.map((artifact, index) => <section className="peer-artifact" key={artifact.artifact_id}
      aria-label={artifact.name || `Материал ${index + 1}`}>
      <h4>{artifact.name || `Материал ${index + 1}`}</h4>
      <ul className="message-attachments response-files">
        {artifact.parts.map((part) => <FileCard key={part.file_id} api={api} contextId={contextId}
          taskId={taskId} file={part} partLabel={part.type === "file" ? undefined : labels[part.type]} />)}
      </ul>
    </section>)}
  </div>;
}
