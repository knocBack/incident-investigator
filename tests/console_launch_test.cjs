// Exercise the shipped browser script against delayed HTTP responses, without a browser or provider.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(process.argv[2], "utf8");
const watchdog = setTimeout(() => { process.stderr.write("launch regression timed out\n"); process.exit(1); }, 10000);
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(value = "") {
    this.value = value; this.dataset = {}; this.listeners = {}; this.children = [];
    this.labels = [{textContent: "field"}]; this.textContent = "";
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  emit(name) { return this.listeners[name]?.({preventDefault() {}, submitter: this}); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  replaceChildren() { this.children = []; }
  append(...items) { this.children.push(...items); }
  querySelectorAll() { return []; }
}
function setup() {
  const form = new Element(); form.dataset.enabled = "yes";
  form.elements = Object.fromEntries(Object.entries({command:"eval-suite",provider:"scripted",model_id:"",case_set:"primary",system:"multi",limits_profile:"demo",repeats:"1",concurrency:"2",budget_usd:"0",run_budget_usd:"",call_seconds:"",wall_seconds:"",max_input_tokens:"64000",run:"",trace:"",confirm_paid:""}).map(([k,v])=>[k,new Element(v)]));
  form.elements.namedItem = name => form.elements[name];
  form.elements.limits_profile.selectedOptions = [{value:"demo",dataset:{callSeconds:30,wallSeconds:240}}];
  const nodes = Object.fromEntries(["suite-budget-warning","run-error","command-preview","launch-job","launch-status","run-error-fields","reservation-estimate","replay-note","job-limit","retry-preview"].map(k=>[k,new Element()]));
  nodes["run-form"] = form;
  const document = {
    querySelector: () => ({content:"local-token"}), querySelectorAll: () => [],
    getElementById: name => nodes[name] || null, createElement: () => new Element(),
  };
  class FormData {
    constructor() { this.values = Object.fromEntries(Object.entries(form.elements).filter(([,v])=>typeof v !== "function").map(([k,v])=>[k,v.value])); }
    get(name) { return this.values[name]; }
    getAll() { return ["case-02"]; }
    has(name) { return !!this.values[name]; }
  }
  const requests = [], destinations = [];
  const fetch = (url, options) => new Promise(resolve => requests.push({url,body:JSON.parse(options.body),resolve}));
  vm.runInNewContext(script, {document,FormData,fetch,location:{assign:url=>destinations.push(url)}});
  const reply = (request, changes = {}, ok = true) => request.resolve({ok,json:async()=>({command:"current command",plan:{timeout_seconds:300,attempts:1,concurrency:1,termination_grace_seconds:120},estimated_call_reservation:null,launch_allowed:true,authorization_required:false,blocked_fields:[],...changes})});
  return {form,nodes,requests,destinations,reply};
}
(async () => {
  // A click while a check is pending must launch once, using the latest form values.
  const a = setup();
  a.form.elements.call_seconds.value = "180";
  a.form.emit("input");
  a.form.emit("change"); // Blur on the way to Launch, with identical values.
  assert.equal(a.requests.length, 2, "blur reuses the input preview");
  assert.equal(a.nodes["launch-job"].disabled, false, "pending check accepts the first click");
  assert.match(a.nodes["launch-status"].textContent, /Updating command/);
  const launch = a.form.emit("submit");
  a.form.emit("submit"); // No duplicate launch while waiting.
  assert.equal(a.requests.filter(r=>r.url === "/api/jobs").length, 0);
  // Edit again while Launch waits. The earlier response cannot authorize stale settings.
  a.form.elements.wall_seconds.value = "600"; a.form.emit("input");
  a.reply(a.requests[1]); await flush();
  assert.equal(a.requests.filter(r=>r.url === "/api/jobs").length, 0);
  a.reply(a.requests[2]); await flush();
  assert.match(a.nodes["job-limit"].textContent, /effective concurrency 1 \(requested 2; limited by available attempts\)/);
  const jobs = a.requests.filter(r=>r.url === "/api/jobs");
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].body.call_seconds, 180); assert.equal(jobs[0].body.wall_seconds, 600);
  a.reply(a.requests[0]); // A very late original preview must not trigger another launch.
  a.reply(jobs[0], {url:"/jobs/first-click"}); await launch;
  assert.deepEqual(a.destinations, ["/jobs/first-click"]);
  assert.equal(a.requests.filter(r=>r.url === "/api/jobs").length, 1);

  // Free calls still require authorization, with the exact reason beside the button.
  const b = setup(); const blocked = b.form.emit("submit");
  b.reply(b.requests[0], {authorization_required:true,free_model:true}); await blocked;
  assert.equal(b.requests.length, 1); assert.equal(b.nodes["launch-job"].disabled, true);
  assert.match(b.nodes["launch-status"].textContent, /Zero-price models can use cap 0/);
  assert.match(b.nodes["launch-status"].textContent, /still require this authorization/);
  b.form.elements.confirm_paid.value = "on"; b.form.emit("change");
  const confirmed = b.form.emit("submit"); b.reply(b.requests[1]); await flush();
  const permitted = b.requests.find(r=>r.url === "/api/jobs");
  assert.equal(permitted.body.confirm_paid, true);
  b.reply(permitted, {url:"/jobs/authorized"}); await confirmed;

  // The live preview hides its retry control unless a settings check fails.
  const c = setup(); const refused = c.form.emit("submit");
  c.reply(c.requests[0], {detail:"Price catalogue unavailable"}, false); await refused;
  assert.equal(c.requests.length, 1); assert.equal(c.nodes["launch-job"].disabled, true);
  assert.match(c.nodes["launch-status"].textContent, /Price catalogue unavailable/);
  assert.equal(c.nodes["retry-preview"].hidden, false);
  c.nodes["retry-preview"].emit("click"); assert.equal(c.requests.length, 2);
  assert.equal(c.nodes["retry-preview"].hidden, true);
  c.reply(c.requests[1], {launch_allowed:false,message:"Raise Suite cap USD",blocked_fields:["budget_usd"]}); await flush();
  assert.match(c.nodes["launch-status"].textContent, /Raise Suite cap USD/);
  assert.equal(c.nodes["launch-job"].disabled, true);
  // A refused launch must not strand the button: once the condition clears, Launch works again.
  const d = setup(); d.reply(d.requests[0]); await flush();
  const first = d.form.emit("submit"); await flush();
  const attempt = d.requests.filter(r=>r.url === "/api/jobs");
  assert.equal(attempt.length, 1);
  d.reply(attempt[0], {detail:"a console job is already active"}, false); await first;
  assert.equal(d.nodes["launch-job"].disabled, false, "refusal leaves Launch usable");
  assert.match(d.nodes["launch-status"].textContent, /already active/);
  const second = d.form.emit("submit"); await flush();
  assert.equal(d.nodes["run-error"].textContent, "", "stale refusal clears on retry");
  const retry = d.requests.filter(r=>r.url === "/api/jobs");
  assert.equal(retry.length, 2);
  d.reply(retry[1], {url:"/jobs/after-refusal"}); await second;
  assert.deepEqual(d.destinations, ["/jobs/after-refusal"]);

  // A late cancel acknowledgement cannot overwrite an already-final SSE status.
  const cancelButton = new Element(), cancelStatus = new Element();
  cancelButton.dataset.job = "child";
  let acknowledge;
  const cancelDocument = {
    querySelector: () => ({content:"local-token"}), querySelectorAll: () => [],
    getElementById: name => ({"cancel-job":cancelButton,"cancel-status":cancelStatus})[name] || null,
  };
  vm.runInNewContext(script, {document:cancelDocument,window:{confirm:message=>{
    assert.match(message, /Running attempts will stop/);
    assert.match(message, /Results so far will be kept/);
    assert.match(message, /Spend already incurred still applies/);
    return true;
  }},fetch:(url, options)=> {
    assert.equal(url, "/api/jobs/child/cancel"); assert.equal(JSON.parse(options.body).confirm, true);
    return new Promise(resolve=>acknowledge=resolve);
  }});
  const cancellation = cancelButton.emit("click");
  cancelButton.hidden = true; cancelStatus.textContent = "Cancelled by user; graceful finalization finished.";
  acknowledge({ok:true,json:async()=>({status:"cancelling"})}); await cancellation;
  assert.match(cancelStatus.textContent, /finalization finished/);
  // A natural finish wins a cancel race, including a late refused-cancel response.
  const jobNodes = Object.fromEntries(["cancel-job","cancel-status","job-output","job-status","job-outcome","job-exit","job-runs"].map(k=>[k,new Element()]));
  jobNodes["cancel-job"].dataset.job = "natural";
  jobNodes["job-output"].dataset.job = "natural";
  let stream, refuse;
  const jobDocument = {querySelector:()=>({content:"local-token"}),querySelectorAll:()=>[],getElementById:name=>jobNodes[name] || null,createElement:()=>new Element()};
  vm.runInNewContext(script, {document:jobDocument,window:{confirm:()=>true},EventSource:class {constructor(){stream=this;} close(){}},fetch:()=>new Promise(resolve=>refuse=resolve)});
  const raced = jobNodes["cancel-job"].emit("click");
  stream.onmessage({data:JSON.stringify({status:"completed",exit_code:0,runs:[{id:"retained",fault:"case",system:"single",status:"interrupted",execution_label:"cancelled in flight",verdict:"no_report"}]})});
  refuse({ok:false,json:async()=>({detail:"Only a running job can be cancelled"})}); await raced;
  assert.equal(jobNodes["cancel-status"].textContent, "");
  assert.equal(jobNodes["job-status"].textContent, "completed");
  assert.equal(jobNodes["job-exit"].textContent, "Exit code: 0");
  const reportLinks = jobNodes["job-runs"].children[0];
  assert.equal(reportLinks.children[0].textContent, "1 retained run reports");
  assert.match(reportLinks.children[1].children[0].textContent, /cancelled in flight/);
  assert.match(reportLinks.children[1].children[1].textContent, /unknown model \/ unknown tool calls/);
  const manyRuns = Array.from({length:47}, (_,i)=>({id:String(i),status:"completed",model_calls:8,tool_calls:3}));
  stream.onmessage({data:JSON.stringify({status:"completed",runs:manyRuns})});
  const manyLinks = jobNodes["job-runs"].children[0];
  assert.equal(manyLinks.open, false);
  assert.equal(manyLinks.children[0].textContent, "47 retained run reports");
  assert.equal(manyLinks.children.length, 48);
  assert.match(manyLinks.children[1].children[1].textContent, /8 model \/ 3 tool calls/);
  manyLinks.open = true;
  jobNodes["job-report-links"] = manyLinks;
  stream.onmessage({data:JSON.stringify({status:"completed",runs:manyRuns})});
  assert.equal(jobNodes["job-runs"].children[0].open, true);
  stream.onmessage({data:JSON.stringify({status:"cancelled",exit_code:143})});
  assert.match(jobNodes["job-exit"].textContent, /Cancelled by user/);
  assert.doesNotMatch(jobNodes["job-exit"].textContent, /Exit code/);
  process.stdout.write("first-click launch, latest-settings check, authorization and retry passed\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
