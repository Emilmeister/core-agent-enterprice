import type { Preview } from "@storybook/react-vite";
import "../../ui/src/styles.css";
import "../stories/catalog.css";

const preview: Preview = {
  parameters: { layout: "padded", controls: { expanded: true },
    options: { storySort: { order: ["Обзор", "Чат", "HITL", "Действия", "Файлы", "Агенты", "Экраны"] } } },
};
export default preview;
