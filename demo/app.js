"use strict";
let data = null;
let state = { journey: null, stepIndex: 0, waitingStep: null, timer: null };
const messagesEl = document.getElementById("messages");
const typingEl = document.getElementById("typing");

function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
function scrollDown() { messagesEl.scrollTop = messagesEl.scrollHeight; }
function addMessage(step) {
  const wrap = el("div", "msg");
  const isBot = step.speaker !== "user";
  wrap.appendChild(el("div", "avatar " + (isBot ? "bot-avatar" : "user-avatar"), isBot ? "PH" : "Y"));
  const body = el("div");
  const author = el("div", "author", isBot ? data.bot.name : data.visitor.name);
  if (isBot) author.appendChild(el("span", "tag", data.bot.tag));
  body.appendChild(author);
  if (step.kind === "embed" || step.kind === "confirm" || step.title) {
    const emb = el("div", "embed" + (step.kind === "confirm" ? " confirm" : ""));
    if (step.title) emb.appendChild(el("strong", null, step.title));
    emb.appendChild(el("p", null, step.text));
    if (step.show_commands) {
      const list = el("ul");
      data.commands.forEach(c => { const li = el("li", null, c.usage + " — " + c.description); list.appendChild(li); });
      emb.appendChild(list);
    }
    body.appendChild(emb);
  } else if (step.kind === "progress") {
    body.appendChild(el("p", null, step.text + ": 0%"));
    const bar = el("div", "progressbar"); const fill = el("i"); fill.style.width = "0%"; bar.appendChild(fill); body.appendChild(bar);
    body.dataset.progress = "1"; body._fill = fill; body._label = body.querySelector("p");
  } else {
    body.appendChild(el("p", null, step.text));
  }
  if (step.buttons && step.buttons.length) {
    const row = el("div");
    step.buttons.forEach(b => {
      const btn = el("button", "chip", b.label); btn.type = "button";
      btn.addEventListener("click", () => { addUserText(b.label); goToStep(state.journey, b.next); });
      row.appendChild(btn);
    });
    body.appendChild(row);
  }
  wrap.appendChild(body); messagesEl.appendChild(wrap); scrollDown();
  return body;
}
function addUserText(text) { addMessage({ speaker: "user", kind: "message", text }); }
function showTyping(ms) { return new Promise(res => { typingEl.hidden = false; setTimeout(() => { typingEl.hidden = true; res(); }, ms); }); }
async function renderStep(step) {
  if (step.speaker === "bot") await showTyping(450);
  const body = addMessage(step);
  if (step.kind === "progress") { runProgress(body, step); return; }
  if (step.buttons && step.buttons.length) { state.waitingStep = step; return; }
  const journey = data.journeys[state.journey];
  const idx = journey.steps.indexOf(step);
  const next = journey.steps[idx + 1];
  if (next) setTimeout(() => renderStep(next), 350);
}
function runProgress(body, step) {
  const percents = step.progress_percents || [0, 100];
  let i = 0;
  const totalMs = 60000; const slice = totalMs / Math.max(1, percents.length - 1);
  state.timer = setInterval(() => {
    i += 1;
    if (i >= percents.length) { clearInterval(state.timer); state.timer = null; goToStep(state.journey, step.next); return; }
    body._fill.style.width = percents[i] + "%";
    body._label.textContent = step.text + ": " + percents[i] + "%";
  }, slice);
}
function goToStep(journeyId, stepId) {
  const journey = data.journeys[journeyId]; if (!journey) return;
  const step = journey.steps.find(s => s.id === stepId); if (!step) return;
  state.journey = journeyId; renderStep(step);
}
function startJourney(journeyId) {
  resetMessages();
  const journey = data.journeys[journeyId]; if (!journey) return;
  state.journey = journeyId; renderStep(journey.steps[0]);
}
function resetMessages() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  messagesEl.textContent = ""; state.waitingStep = null;
  addMessage({ speaker: "bot", kind: "message", text: "Welcome to the Print Handy demo. Pick a sample above, or type a message. Nothing here prints — the printer is simulated and the data is sample data only." });
}
function matchIntent(text) {
  const t = text.toLowerCase();
  for (const intent of data.intents) { if (intent.patterns.some(p => t.includes(p))) return intent.journey; }
  return "fallback";
}
async function init() {
  const res = await fetch("scenarios.json");
  data = await res.json();
  const samplesEl = document.getElementById("samples");
  data.samples.forEach(s => {
    const b = el("button", null, s.name); b.type = "button"; b.title = s.note;
    b.addEventListener("click", () => startJourney(s.journey)); samplesEl.appendChild(b);
  });
  document.getElementById("reset").addEventListener("click", () => { resetMessages(); });
  document.getElementById("composer").addEventListener("submit", e => {
    e.preventDefault();
    const input = document.getElementById("input"); const text = input.value.trim(); if (!text) return;
    input.value = ""; resetMessagesSilent(); addUserText(text); state.journey = matchIntent(text);
    const journey = data.journeys[state.journey];
    const firstBot = journey.steps.find(s => s.speaker === "bot") || journey.steps[0];
    renderStep(firstBot);
  });
  const fileInput = document.getElementById("file");
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) theatreFile(fileInput.files[0]); });
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("drop", e => { e.preventDefault(); if (e.dataTransfer.files[0]) theatreFile(e.dataTransfer.files[0]); });
  resetMessages();
}
function resetMessagesSilent() { if (state.timer) { clearInterval(state.timer); state.timer = null; } messagesEl.textContent = ""; }
function theatreFile(file) {
  resetMessagesSilent();
  addUserText(file.name + " (" + file.size + " bytes)");
  addMessage({ speaker: "bot", kind: "message", text: "I can see the file name and size, but this demo does not parse visitor files. Use one of the pre-seeded samples above to play the flow." });
}
init();
