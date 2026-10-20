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
  const launch = document.getElementById("launch-job");
  let launching = false;
  function blockedFields(names) {
    for (const input of form.querySelectorAll('[aria-invalid="true"]')) input.removeAttribute("aria-invalid");
    const links = document.getElementById("run-error-fields");
    links.replaceChildren();
    for (const name of names) {
      const input = form.elements.namedItem(name);
      if (!input) continue;
      input.id ||= name;
      input.setAttribute("aria-invalid", "true");
      const link = document.createElement("a");
      link.href = "#" + input.id;
      link.textContent = "Go to " + (input.labels[0]?.textContent.trim() || name);
      link.className = "button small";
      link.addEventListener("click", (event) => {
        event.preventDefault();
        input.scrollIntoView({ block: "center", behavior: "smooth" });
        input.focus({ preventScroll: true });
      });
      links.append(link);
    }
  }
  function profilePlaceholders() {
    const option = form.elements.limits_profile.selectedOptions[0];
    form.elements.call_seconds.placeholder = `${option.dataset.callSeconds} seconds (${option.value})`;
    form.elements.wall_seconds.placeholder = `${option.dataset.wallSeconds} seconds (${option.value})`;
  }
  function body() {
    const data = new FormData(form);
    return {
      command: data.get("command"),
      faults: data.getAll("faults"),
      provider: data.get("provider"),
      model_id: data.get("model_id"),
      graph: "staged",
      case_set: data.get("case_set"),
      system: data.get("system"),
      limits_profile: data.get("limits_profile"),
      repeats: Number(data.get("repeats")),
      concurrency: Number(data.get("concurrency")),
      budget_usd: data.get("budget_usd"),
      run_budget_usd: data.get("run_budget_usd") || null,
      call_seconds: data.get("call_seconds") ? Number(data.get("call_seconds")) : null,
      wall_seconds: data.get("wall_seconds") ? Number(data.get("wall_seconds")) : null,
      max_input_tokens: Number(data.get("max_input_tokens")),
      confirm_paid: data.has("confirm_paid"),
      trace: data.has("trace"),
      run: data.get("run") || null,
    };
  }
  let previewVersion = 0;
  async function update() {
    const version = ++previewVersion;
    launch.disabled = true;
    profilePlaceholders();
    document.getElementById("reservation-estimate").textContent = form.elements.provider.value === "openrouter" ? "Checking model prices and initial-call reservation..." : "This replay makes no model calls.";
    document.getElementById("replay-note").hidden = form.elements.provider.value !== "scripted" || form.elements.command.value === "eval";
    try {
      const result = await post("/api/preview", body());
      if (version === previewVersion) {
        preview.textContent = result.command;
        document.getElementById("job-limit").textContent =
          `Job limit: ${result.plan.timeout_seconds} seconds for ${result.plan.attempts} attempts at concurrency ${result.plan.concurrency}; then up to ${result.plan.termination_grace_seconds} seconds for graceful finalization.`;
        document.getElementById("reservation-estimate").textContent = result.estimated_call_reservation === null
          ? (form.elements.provider.value === "scripted" || form.elements.command.value === "eval" ? "This replay or saved-run grading makes no model calls." : "Reservation unavailable until prices and settings can be checked.")
          : result.reservation_guide;
        error.textContent = result.launch_allowed ? "" : result.message;
        blockedFields(result.blocked_fields || []);
        launch.disabled = launching || !result.launch_allowed;
      }
    } catch (e) {
      if (version === previewVersion) {
        preview.textContent = "Resolve settings to preview the command.";
        document.getElementById("job-limit").textContent = "Job limit unavailable until settings are valid.";
        error.textContent = e.message;
        blockedFields([]);
      }
    }
  }
  document.getElementById("preview-command").addEventListener("click", update);
  function selectSet() {
    for (const input of form.querySelectorAll('input[name="faults"]')) {
      const allowed = form.elements.case_set.value === "all" || input.dataset.set === form.elements.case_set.value;
      input.closest("label").hidden = !allowed;
      if (!allowed) input.checked = false;
    }
  }
  form.elements.case_set.addEventListener("change", selectSet);
  selectSet();
  form.addEventListener("change", update);
  form.addEventListener("input", update);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    if (button.disabled) return;
    launching = true;
    button.disabled = true;
    try {
      const result = await post("/api/jobs", body());
      location.assign(result.url);
    } catch (e) {
      error.textContent = e.message;
      launching = false;
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
    document.getElementById("job-outcome").textContent = job.outcome || "";
    document.getElementById("job-outcome").className = "callout " + (job.outcome_tone === "warning" ? "warning" : "info");
    output.replaceChildren();
    for (const line of (job.output || "").split("\n")) {
      const run = (job.runs || []).find(r => r.report_path && line.includes(r.report_path));
      if (run) {
        const index = line.indexOf(run.report_path), link = document.createElement("a");
        link.href = "/runs/" + encodeURIComponent(run.id) + "?tab=report";
        link.textContent = "Open report · " + run.fault + " · " + run.system;
        output.append(line.slice(0, index), link, line.slice(index + run.report_path.length), "\n");
      } else output.append(line + "\n");
    }
    document.getElementById("job-exit").textContent =
      job.exit_code === undefined
        ? job.error || ""
        : "Exit code: " + job.exit_code;
    if (job.status !== "running") stream.close();
    const runs = document.getElementById("job-runs");
    runs.replaceChildren();
    for (const suite of job.suites || []) {
      const p = document.createElement("p"), a = document.createElement("a");
      a.href = "/suites/" + encodeURIComponent(suite.id);
      a.textContent = "Open comparison · " + suite.name;
      a.className = "button primary";
      p.append(a); runs.append(p);
    }
    for (const run of job.runs || []) {
      const p = document.createElement("p"), a = document.createElement("a");
      a.href = "/runs/" + encodeURIComponent(run.id) + "?tab=report";
      a.textContent = ["Open report", run.fault, run.system, run.status, run.verdict].join(" · ");
      p.append(a);
      if (run.phoenix_url) {
        const trace = document.createElement("a");
        trace.href = run.phoenix_url; trace.target = "_blank"; trace.rel = "noreferrer";
        trace.textContent = "Open in Phoenix"; trace.className = "button small";
        p.append(" ", trace);
      }
      runs.append(p);
    }
    if (!(job.runs || []).length) {
      const p = document.createElement("p");
      p.textContent = job.status === "running" ? "Waiting for the first admitted run. Progress and links appear here automatically." : "Open the comparison above for skipped attempts and reasons. Grading jobs update the existing run.";
      runs.append(p);
    }
  };
  stream.onerror = () => {
    document.getElementById("job-exit").textContent =
      "Stream disconnected. Reload to reconnect; the job continues on the server.";
  };
}
