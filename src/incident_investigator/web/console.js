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
      link.textContent = "Go to " + (input.dataset.label || input.labels[0]?.textContent.trim().split("\n")[0] || name);
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
  let previewVersion = 0, previewKey = "", previewPromise = null;
  let pending = false, checked = null, launchPhase = "";
  const launchStatus = document.getElementById("launch-status");
  function renderLaunch() {
    launch.disabled = launching || (!pending && (!checked?.launch_allowed || checked.authorization_required));
    launch.textContent = launching ? launchPhase : pending ? "Launch after check" : "Launch";
    launchStatus.textContent = launching ? launchPhase : pending
      ? "Updating command… Launch will wait for the current settings check."
      : launch.disabled ? error.textContent || "Resolve settings before launching."
      : error.textContent ? error.textContent + " Launch again once that is resolved."
      : "Ready to launch the current settings.";
  }
  function update(force = false) {
    const request = body(), key = JSON.stringify(request);
    // A blur/change after input must reuse the same check instead of swallowing the click.
    if (!force && key === previewKey && previewPromise) return previewPromise;
    previewKey = key;
    const version = ++previewVersion;
    pending = true;
    renderLaunch();
    preview.textContent = "Preview out of date; refreshing from current settings...";
    const priorBudgetWarning = document.getElementById("suite-budget-warning");
    priorBudgetWarning.textContent = "";
    priorBudgetWarning.hidden = true;
    profilePlaceholders();
    document.getElementById("reservation-estimate").textContent = form.elements.provider.value === "openrouter" ? "Checking model prices and initial-call reservation..." : "This replay makes no model calls.";
    document.getElementById("replay-note").hidden = form.elements.provider.value !== "scripted" || form.elements.command.value === "eval";
    previewPromise = (async () => {
      try {
        const result = await post("/api/preview", request);
        if (version === previewVersion) {
          checked = result;
          pending = false;
          preview.textContent = result.command;
          document.getElementById("job-limit").textContent =
            `Job limit: ${result.plan.timeout_seconds} seconds for ${result.plan.attempts} attempts at concurrency ${result.plan.concurrency}; then up to ${result.plan.termination_grace_seconds} seconds for graceful finalization.`;
          document.getElementById("reservation-estimate").textContent = result.estimated_call_reservation === null
            ? (form.elements.provider.value === "scripted" || form.elements.command.value === "eval" ? "This replay or saved-run grading makes no model calls." : "Reservation unavailable until prices and settings can be checked.")
            : result.reservation_guide;
          const budgetWarning = document.getElementById("suite-budget-warning");
          budgetWarning.textContent = result.budget_warning || "";
          budgetWarning.hidden = !result.budget_warning;
          const authorization = result.authorization_required
            ? "Tick 'I explicitly authorize OpenRouter calls' in section 02. Zero-price models can use cap 0 and still require this authorization."
            : "";
          error.textContent = [result.launch_allowed ? "" : result.message, authorization].filter(Boolean).join(" ");
          blockedFields([...(result.blocked_fields || []), ...(result.authorization_required ? ["confirm_paid"] : [])]);
          renderLaunch();
        }
        return result;
      } catch (e) {
        if (version === previewVersion) {
          checked = null;
          pending = false;
          preview.textContent = "Resolve settings to preview the command.";
          document.getElementById("job-limit").textContent = "Job limit unavailable until settings are valid.";
          error.textContent = e.message;
          blockedFields([]);
          renderLaunch();
        }
        return null;
      }
    })();
    return previewPromise;
  }
  document.getElementById("preview-command").addEventListener("click", () => update(true));
  function selectSet() {
    for (const input of form.querySelectorAll('input[name="faults"]')) {
      const allowed = form.elements.case_set.value === "all" || input.dataset.set === form.elements.case_set.value;
      input.closest("label").hidden = !allowed;
      if (!allowed) input.checked = false;
    }
  }
  form.elements.case_set.addEventListener("change", selectSet);
  selectSet();
  form.addEventListener("change", () => update());
  form.addEventListener("input", () => update());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (launching) return;
    launching = true;
    if (!launch.disabled) error.textContent = ""; // Drop a stale refusal; blocking check messages stay.
    launchPhase = "Checking settings…";
    renderLaunch();
    try {
      let request, validation, checking;
      do {
        request = body();
        checking = update();
        validation = await checking;
      } while (checking !== previewPromise || JSON.stringify(request) !== JSON.stringify(body()));
      if (!validation?.launch_allowed || validation.authorization_required) {
        launching = false;
        renderLaunch();
        return;
      }
      launchPhase = "Launching…";
      renderLaunch();
      const result = await post("/api/jobs", request);
      location.assign(result.url);
    } catch (e) {
      error.textContent = e.message;
      if (e.message.includes("explicitly authorize OpenRouter")) blockedFields(["confirm_paid"]);
      launching = false;
      renderLaunch();
    }
  });
  update();
}
const cancel = document.getElementById("cancel-job");
if (cancel) cancel.addEventListener("click", async () => {
  if (!window.confirm("Cancel this job? The running attempt will stop; graceful shutdown will retain results and reconcile spend where available.")) return;
  cancel.disabled = true;
  const status = document.getElementById("cancel-status");
  status.textContent = "Requesting cancellation…";
  try {
    await post("/api/jobs/" + encodeURIComponent(cancel.dataset.job) + "/cancel", {confirm: true});
    if (!cancel.hidden) status.textContent = "Cancellation requested; waiting for graceful finalization.";
  } catch (error) {
    status.textContent = error.message;
  }
});
const output = document.getElementById("job-output");
if (output) {
  const stream = new EventSource("/api/jobs/" + output.dataset.job + "/events");
  stream.onmessage = (event) => {
    const job = JSON.parse(event.data);
    document.getElementById("job-status").textContent = job.heading || job.status;
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
    if (cancel) {
      cancel.hidden = !job.can_cancel && job.status !== "cancelling";
      cancel.disabled = !job.can_cancel;
      cancel.textContent = job.status === "cancelling" ? "Cancelling…" : "Cancel job";
      if (job.status === "cancelled") document.getElementById("cancel-status").textContent = job.termination === "SIGKILL" ? "Cancelled by user; forced stop required." : "Cancelled by user; graceful finalization finished.";
    }
    if (!["running", "cancelling"].includes(job.status)) stream.close();
    const runs = document.getElementById("job-runs");
    runs.replaceChildren();
    for (const suite of job.suites || []) {
      const p = document.createElement("p"), a = document.createElement("a");
      a.href = "/suites/" + encodeURIComponent(suite.id);
      a.textContent = (suite.results_label || "Open results") + " · " + (suite.title || suite.name);
      a.className = "button primary";
      p.append(a); runs.append(p);
    }
    for (const run of job.runs || []) {
      const p = document.createElement("p"), a = document.createElement("a");
      a.href = "/runs/" + encodeURIComponent(run.id) + "?tab=report";
      a.textContent = ["Open report", run.fault, run.system, run.status, run.verdict].join(" · ");
      p.append(a);
      const progress = document.createElement("small");
      progress.textContent = run.outcome || run.progress || "Progress unavailable";
      p.append(progress);
      if (run.phoenix_url) {
        const trace = document.createElement("a");
        trace.href = run.phoenix_url; trace.target = "_blank"; trace.rel = "noreferrer";
        trace.textContent = "Phoenix · live detailed view"; trace.className = "button small";
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
