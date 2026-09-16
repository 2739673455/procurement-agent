import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const container = document.createElement("div");
container.id = "buying_ai_root";
document.body.append(container);
createRoot(container).render(createElement(App));
