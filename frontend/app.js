/* Test frontend: upload → plan → voice interview with live chat → results.
   Plain JS on purpose; state lives in `interviewId` + polling + text streams. */

const $ = (id) => document.getElementById(id);

// Prefixed logger: filter the console with "[app]" to see only these.
const log = (...args) => console.log("[app]", ...args);

let interviewId = null;
let pollTimer = null;
let room = null;
let lastInterview = null;
let interviewEnded = false;

// ---- View switching ---------------------------------------------------------

function show(view) {
  for (const id of ["upload-view", "session-view"]) {
    $(id).hidden = id !== view;
  }
}

function showPanel(panel) {
  $("interview-panel").hidden = panel !== "interview";
  $("results-panel").hidden = panel !== "results";
}

// ---- Polling ----------------------------------------------------------------

async function fetchInterview() {
  const res = await fetch(`/interviews/${interviewId}`);
  if (!res.ok) throw new Error(`GET interview failed: ${res.status}`);
  return res.json();
}

function startPolling(onUpdate) {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      const interview = await fetchInterview();
      lastInterview = interview;
      onUpdate(interview);
    } catch (err) {
      console.error("[app] polling failed:", err);
    }
  }, 2000);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

// ---- 1: Upload --------------------------------------------------------------

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("upload-btn");
  button.disabled = true;
  $("upload-status").textContent =
    "Reading the resume and planning the interview… (can take ~1 min)";
  try {
    log("uploading resume and requesting a plan…");
    const res = await fetch("/interviews", {
      method: "POST",
      body: new FormData(event.target),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const interview = await res.json();
    interviewId = interview.id;
    lastInterview = interview;
    log("interview created:", interview.id, `(${interview.milestones.length} milestones)`);
    renderPlan(interview);
    show("session-view");
    showPanel("interview");
  } catch (err) {
    console.error("[app] interview creation failed:", err);
    $("upload-status").textContent = `Error: ${err.message}`;
  } finally {
    button.disabled = false;
  }
});

// ---- Milestone sidebar ------------------------------------------------------

function renderPlan(interview) {
  const list = $("milestone-list");
  list.innerHTML = "";
  for (const m of interview.milestones) {
    const li = document.createElement("li");
    li.textContent = m.title;
    li.title = m.description; // full text on hover only
    li.dataset.id = m.id;
    list.appendChild(li);
  }
  updateMilestones(interview);
}

function updateMilestones(interview) {
  if (["completed", "evaluated"].includes(interview.status)) interviewEnded = true;
  for (const m of interview.milestones) {
    const li = document.querySelector(`#milestone-list li[data-id="${m.id}"]`);
    if (!li) continue;
    li.classList.toggle("done", m.completed);
    li.classList.toggle("missed", !m.completed && interviewEnded);
  }
}

// ---- Agent state badge (listening / thinking / speaking) --------------------

const AGENT_STATE_LABELS = {
  initializing: "Connecting…",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

function setAgentState(state) {
  const badge = $("agent-state");
  const label = AGENT_STATE_LABELS[state];
  badge.hidden = !label || interviewEnded;
  if (label) {
    badge.textContent = label;
    badge.dataset.state = state;
  }
}

function registerAgentStateHandler(room) {
  // The agent publishes its lifecycle in the `lk.agent.state` participant
  // attribute; mirroring it fills the dead air while the LLM thinks.
  room.on(LivekitClient.RoomEvent.ParticipantAttributesChanged, (changed, participant) => {
    if (!participant.isLocal && "lk.agent.state" in changed) {
      log("agent state:", changed["lk.agent.state"]);
      setAgentState(changed["lk.agent.state"]);
    }
  });
  room.on(LivekitClient.RoomEvent.ParticipantConnected, (participant) => {
    const state = participant.attributes?.["lk.agent.state"];
    if (state) setAgentState(state);
  });
}

// ---- Live chat (LiveKit transcription text streams) -------------------------

const segments = new Map(); // segmentId -> { el, text, who }
const lastSegment = {}; // who -> most recent segment (for supersede dedupe)

function bubbleFor(segmentId, who) {
  let seg = segments.get(segmentId);
  if (!seg) {
    seg = { el: null, text: "", who };
    segments.set(segmentId, seg);
  }
  return seg;
}

// Bubbles are created lazily on first text: preemptive generations that get
// discarded (or empty STT streams) would otherwise leave ghost bubbles.
function setBubbleText(seg, text) {
  if (!text) return;
  if (!seg.el) {
    $("interview-status").textContent = "";
    seg.el = document.createElement("div");
    seg.el.className = `bubble ${seg.who} interim`;
    $("chat-log").appendChild(seg.el);
    lastSegment[seg.who] = seg;
  }
  seg.text = text;
  seg.el.textContent = text;
  scrollChat();
}

function finalizeBubble(seg) {
  clearTimeout(seg.timer);
  if (!seg.el) return;
  // Supersede dedupe: if the previous bubble from the same speaker never
  // finalized and one text is a prefix of the other, it was a discarded
  // partial of this same utterance — drop it.
  const prev = lastSegment[seg.who];
  if (prev && prev !== seg && prev.el && prev.el.classList.contains("interim")) {
    if (seg.text.startsWith(prev.text) || prev.text.startsWith(seg.text)) {
      prev.el.remove();
      prev.el = null;
    }
  }
  seg.el.classList.remove("interim");
  lastSegment[seg.who] = seg;
}

function scrollChat() {
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
}

function registerTranscriptionHandler(room) {
  // One handler per topic per Room (a second registration throws).
  room.registerTextStreamHandler("lk.transcription", async (reader, participantInfo) => {
    const attrs = reader.info.attributes ?? {};
    const segmentId = attrs["lk.segment_id"] ?? reader.info.id;
    if (!attrs["lk.segment_id"]) console.warn("transcription stream without lk.segment_id");
    const isUser = participantInfo.identity === "candidate";
    const seg = bubbleFor(segmentId, isUser ? "user" : "agent");

    if (isUser) {
      // User STT: each interim is a separate stream carrying the FULL text so
      // far under the same segment id — replace the bubble text in place.
      const text = await reader.readAll();
      setBubbleText(seg, text);
      if (attrs["lk.transcription_final"] === "true") {
        finalizeBubble(seg);
      } else {
        // Safety net: if the final version never arrives (it occasionally
        // doesn't), solidify the interim after 3s without updates.
        clearTimeout(seg.timer);
        seg.timer = setTimeout(() => finalizeBubble(seg), 3000);
      }
    } else {
      // Agent speech: one delta stream per segment, word-synced with the
      // audio playback — accumulate chunks; stream close = segment done
      // (works for interruptions too; the trailer "final" attribute isn't
      // reliably surfaced by the JS SDK).
      for await (const chunk of reader) {
        setBubbleText(seg, seg.text + chunk);
      }
      if (seg.el) finalizeBubble(seg);
      else segments.delete(segmentId); // empty stream: never render it
    }
  });
}

// ---- 2: Interview -----------------------------------------------------------

$("start-btn").addEventListener("click", async () => {
  // Must run inside the click handler: mic permission + audio autoplay.
  const button = $("start-btn");
  button.disabled = true;
  $("interview-status").textContent = "Connecting…";
  try {
    const res = await fetch(`/interviews/${interviewId}/token`);
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const { server_url, token, room: roomName } = await res.json();
    log("token received, connecting to room:", roomName);

    room = new LivekitClient.Room({
      // Cleaner mic input = better STT = better interview.
      audioCaptureDefaults: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      publishDefaults: { dtx: true },
    });
    registerTranscriptionHandler(room);
    registerAgentStateHandler(room);
    room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === "audio") $("audio-container").appendChild(track.attach());
    });
    room.on(LivekitClient.RoomEvent.Disconnected, (reason) => {
      // Interview over (agent deleted the room, or connection lost).
      log("disconnected from room (reason:", reason, ") — showing results");
      interviewEnded = true;
      $("agent-state").hidden = true;
      if (lastInterview) updateMilestones(lastInterview);
      stopPolling();
      showResults();
    });

    await room.connect(server_url, token);
    await room.localParticipant.setMicrophoneEnabled(true);
    log("connected, microphone enabled");
    button.hidden = true;
    $("interview-status").textContent =
      "Connected — the interviewer will greet you in a few seconds. Speak into your microphone.";
    startPolling(updateMilestones);
  } catch (err) {
    console.error("[app] could not start the interview:", err);
    $("interview-status").textContent = `Error: ${err.message}`;
    button.disabled = false;
  }
});

// ---- 3: Results -------------------------------------------------------------

// ~3 min at the 2s poll interval. Evaluation normally lands well before, but
// the worker's auto-trigger can die silently (e.g. server was down at that
// moment) — the endpoint is re-invocable, so offer a manual retry after this.
const MAX_EVAL_POLLS = 90;
let evalPolls = 0;

function showResults() {
  showPanel("results");
  startEvalPolling();
}

function startEvalPolling() {
  evalPolls = 0;
  $("results-error").hidden = true;
  $("retry-eval-btn").hidden = true;
  $("results-status").hidden = false;
  startPolling((interview) => {
    updateMilestones(interview);
    if (interview.status === "evaluated" && interview.evaluation) {
      stopPolling();
      log("evaluation received:", interview.evaluation.hired ? "HIRED" : "NOT HIRED",
          `score ${interview.evaluation.score}/100`);
      renderEvaluation(interview);
      return;
    }
    if (interview.status === "evaluation_failed") {
      stopPolling();
      showEvalError("The evaluation failed. You can retry it.");
      return;
    }
    evalPolls += 1;
    if (evalPolls >= MAX_EVAL_POLLS) {
      stopPolling();
      showEvalError("The evaluation is taking longer than expected. You can retry it.");
    }
  });
}

function showEvalError(message) {
  log("evaluation problem:", message);
  $("results-status").hidden = true;
  const err = $("results-error");
  err.textContent = message;
  err.hidden = false;
  $("retry-eval-btn").hidden = false;
}

$("retry-eval-btn").addEventListener("click", async () => {
  const button = $("retry-eval-btn");
  button.disabled = true;
  $("results-error").textContent = "Retrying evaluation… (can take a couple of minutes)";
  try {
    // The response IS the evaluated interview — no need to resume polling.
    const res = await fetch(`/interviews/${interviewId}/evaluate`, { method: "POST" });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const interview = await res.json();
    lastInterview = interview;
    $("results-error").hidden = true;
    button.hidden = true;
    updateMilestones(interview);
    renderEvaluation(interview);
  } catch (err) {
    console.error("[app] evaluation retry failed:", err);
    $("results-error").textContent = `Error: ${err.message}`;
  } finally {
    button.disabled = false;
  }
});

function renderEvaluation(interview) {
  const ev = interview.evaluation;
  $("results-status").hidden = true;
  $("results-box").hidden = false;

  const badge = $("hired-badge");
  badge.textContent = ev.hired ? "HIRED" : "NOT HIRED";
  badge.className = `badge ${ev.hired ? "hired" : "rejected"}`;
  $("score").textContent = `${ev.score}/100`;

  for (const [id, items] of [["strengths", ev.strengths], ["weaknesses", ev.weaknesses]]) {
    const list = $(id);
    list.innerHTML = "";
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    }
  }
  $("rationale").textContent = ev.rationale;
  $("ended-by").textContent = `Interview ended by: ${ev.ended_by}`;
}

$("reset-btn").addEventListener("click", () => window.location.reload());
