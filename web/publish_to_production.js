// Publish to production: the director's note on the node face + a "Refresh shots" button.
// The credential never reaches this file: the backend route answers with shot codes, a state and cleaned note text only.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "ComfyPublish_PublishToProduction";
const PLACEHOLDERS = ["(sign in first)", "(no shots waiting)", "(server unreachable)"];

app.registerExtension({
  name: "comfy.publish_to_production",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_ID) return;
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated ? onCreated.apply(this, arguments) : undefined;
      setup(this);
      return r;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      if (this._pubShow) this._pubShow();            // a loaded graph restores the saved shot after creation
      return r;
    };
  },
});

function setup(node) {
  const combo = node.widgets.find((w) => w.name === "shot");
  if (!combo) return;
  let byValue = {};
  let message = "";

  const el = document.createElement("textarea");
  el.readOnly = true;
  el.rows = 3;
  el.style.cssText = "width:100%;resize:none;box-sizing:border-box;opacity:.9;";
  const view = node.addDOMWidget("note_view", "customtext", el, { serialize: false, getMinHeight: () => 60 });
  view.serialize = false;

  function show() {
    const v = combo.value;
    const s = byValue[v];
    let text;
    if (s) {
      if (s.state === "changes_requested") text = s.note || "Changes requested";
      else if (s.state === "no_version") text = "No version yet";
      else text = "Ready";
    } else if (PLACEHOLDERS.includes(v)) {
      text = message || v;
    } else if (Object.keys(byValue).length) {
      text = "This shot is not in your queue any more (refresh shows the current list)";
    } else {
      text = "Press Refresh shots to read the queue";
    }
    el.value = text;                                   // textContent-equivalent: never HTML
  }
  node._pubShow = show;

  const cb = combo.callback;
  combo.callback = function () {
    const r = cb ? cb.apply(this, arguments) : undefined;
    show();
    return r;
  };

  const btn = node.addWidget("button", "Refresh shots", null, async () => {
    btn.label = "Refreshing...";
    try {
      const res = await api.fetchApi("/publish_to_production/queue");
      const data = await res.json();
      byValue = {};
      for (const s of data.shots || []) byValue[s.value] = s;
      message = data.message || "";
      const values = (data.shots || []).map((s) => s.value);
      if (!values.length) values.push(message || "(no shots waiting)");
      const current = combo.value;
      // Keep the selection: if it vanished, leave it in the list so it shows as missing, never switch shots silently.
      if (current && !values.includes(current)) values.push(current);
      combo.options.values = values;
      show();
      app.graph.setDirtyCanvas(true, true);
    } catch (e) {
      el.value = "Could not refresh the shots";
    } finally {
      btn.label = "Refresh shots";
    }
  }, { serialize: false });
  btn.serialize = false;
  show();
  setTimeout(() => btn.callback(), 0);                 // read the queue once the graph has restored its saved shot
}
