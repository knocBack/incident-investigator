"use strict";
const token = document.querySelector('meta[name="console-token"]').content;
async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Console-Token": token },
    body: JSON.stringify(body),
  });
  const value = await response
    .json()
    .catch(() => ({ detail: "Request failed" }));
  if (!response.ok)
    throw new Error(
      typeof value.detail === "string"
        ? value.detail
        : JSON.stringify(value.detail),
    );
  return value;
}
document
  .querySelectorAll("[data-autosubmit]")
  .forEach((input) =>
    input.addEventListener("change", () => input.form.submit()),
  );
const rubric = document.getElementById("rubric-form");
if (rubric)
  rubric.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = new FormData(rubric),
      dimensions = {};
    [
      "Diagnosis",
      "Support",
      "Contradictions",
      "Uncertainty",
      "Actions",
    ].forEach(
      (name) =>
        (dimensions[name] = {
          score: fields.get(name + "-score"),
          reason: fields.get(name + "-reason"),
        }),
    );
    const status = document.getElementById("rubric-status");
    try {
      await post("/api/runs/" + rubric.dataset.run + "/rubric", { dimensions });
      status.textContent = "Human rubric saved in the workspace.";
    } catch (error) {
      status.textContent = error.message;
    }
  });
const form = document.getElementById("run-form");
if (form && form.dataset.enabled === "yes") {
  const error = document.getElementById("run-error"),
    preview = document.getElementById("command-preview");
  function body() {
    const data = new FormData(form);
    return {
      command: data.get("command"),
      faults: data.getAll("faults"),
      provider: data.get("provider"),
      model_id: data.get("model_id"),
      graph: data.get("graph"),
      limits_profile: data.get("limits_profile"),
      repeats: Number(data.get("repeats")),
      concurrency: Number(data.get("concurrency")),
      budget_usd: data.get("budget_usd"),
      confirm_paid: data.has("confirm_paid"),
      trace: data.has("trace"),
      run: data.get("run") || null,
    };
  }
  let previewVersion = 0;
  async function update() {
    const version = ++previewVersion;
    try {
      const result = await post("/api/preview", body());
      if (version === previewVersion) {
        preview.textContent = result.command;
        error.textContent = "";
      }
    } catch (e) {
      if (version === previewVersion) {
        preview.textContent = "Resolve settings to preview the command.";
        error.textContent = e.message;
      }
    }
  }
  document.getElementById("preview-command").addEventListener("click", update);
  form.addEventListener("change", update);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const result = await post("/api/jobs", body());
      location.assign(result.url);
    } catch (e) {
      error.textContent = e.message;
      button.disabled = false;
    }
  });
  update();
}
const output = document.getElementById("job-output");
if (output) {
  const stream = new EventSource("/api/jobs/" + output.dataset.job + "/events");
  stream.onmessage = (event) => {
    const job = JSON.parse(event.data);
    document.getElementById("job-status").textContent = job.status;
    output.textContent = job.output || "";
    document.getElementById("job-exit").textContent =
      job.exit_code === undefined
        ? job.error || ""
        : "Exit code: " + job.exit_code;
    if (job.status !== "running") {
      stream.close();
      const runs = document.getElementById("job-runs");
      runs.replaceChildren();
      for (const run of job.runs || []) {
        const p = document.createElement("p"),
          a = document.createElement("a");
        a.href = "/runs/" + encodeURIComponent(run.id);
        a.textContent = run.fault + " · " + run.verdict + " · " + run.run_id;
        p.append(a);
        runs.append(p);
      }
      if (!(job.runs || []).length) {
        const p = document.createElement("p");
        p.textContent =
          "No new run artifacts. Grading jobs update the existing run; return to the runs list.";
        runs.append(p);
      }
    }
  };
  stream.onerror = () => {
    document.getElementById("job-exit").textContent =
      "Stream disconnected. Reload to reconnect; the job continues on the server.";
  };
}
