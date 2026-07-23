// S2S (Realtime API) frontend path, WebRTC transport.
//
// Audio flows directly between this browser and OpenAI over WebRTC (see
// connectWebRTC() below) - this backend only mints a short-lived ephemeral
// token (/realtime-token) and hands back session config (/realtime-session-
// config); once connected, it's never in the audio path at all. Business
// logic (state machine, tool calls, email/SMS/calendar effects) still runs
// server-side, via a REST round-trip (/tool-call) triggered by data-channel
// events instead of being handled inline while relaying audio - see the
// "WebRTC transport" section further down for why this replaced the old
// WebSocket relay (it was the source of the choppy/laggy audio).
//
// Reuses the existing DOM (#mic-btn, #dial-status, #transcript,
// #stage-ui, .reading[data-field]) so index.html/style.css don't need a
// parallel UI - only the transport changed.

const API_BASE_URL = window.location.origin;
const SESSION_STORAGE_KEY = "ac_quote_session_id";
// For embedding on a CLIENT's site: <script src=".../realtime-widget.js"
// data-key="pk_live_xxx"></script>. Blank/absent is fine when the
// backend's REQUIRE_API_KEY is off (single-site deployments, the
// current default) - see main.py's _check_api_key.
const API_KEY = document.currentScript?.dataset?.key || "";

const FIELD_LABELS = {
  full_name: "Full name", phone: "Phone", email: "Email",
  street: "Street", city: "City", zip: "Zip", category: "System type",
  tonnage: "Tonnage", location: "Air handler", plan_choice: "Plan",
  // Informational only, on purpose - just the selected action, not every
  // downstream sub-field (call_timing/schedule_appointment/etc).
  plan_action: "Next step",
};
const CATEGORY_LABELS = { heating: "Heating", cooling_electric_heat: "Cooling, electric heat", cooling_heat_pump: "Cooling, heat pump" };
const TONNAGE_LABELS = { "2_ton": "2 ton", "2.5_ton": "2.5 ton", "3_ton": "3 tons", "3.5_ton": "3.5 tons", "4_ton": "4 ton" };
const LOCATION_LABELS = { attic_horizontal: "Attic (horizontal)", closet_vertical: "Closet (vertical)", garage_vertical: "Garage" };
const ACTION_LABELS = { go_with_plan: "Go with this plan", arrange_call: "Arrange a call", arrange_visit: "Arrange a visit" };

let sessionId = null;
let currentStage = null;
let lastPlans = []; // [{id, name}] - resolves plan_choice ids to display names in the sidebar
let currentTools = null; // latest tools_for_stage() payload - threaded into response.create per turn (see createResponse) instead of via session.update, so stage changes don't invalidate the session-level prompt cache
let pushToTalkMode = false;
let pushToTalkActive = false;
let micMuted = true; // starts muted - the mic must never be hot before we deliberately unmute it (see connectWebRTC below)
let intentionalClose = false; // set before a deliberate pc.close() (New session) so reconnect logic doesn't kick in
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_BASE_DELAY_MS = 1000;

const micBtn = document.getElementById("mic-btn");
const dialStatus = document.getElementById("dial-status");
const transcriptEl = document.getElementById("transcript");

function setStatus(text) {
  if (dialStatus) dialStatus.textContent = text;
}

let currentAssistantLine = null;
let currentAssistantText = "";

function appendTranscriptDelta(deltaText) {
  if (!transcriptEl || !deltaText) return;
  if (!currentAssistantLine) {
    transcriptEl.innerHTML = "";
    currentAssistantLine = document.createElement("div");
    currentAssistantLine.className = "transcript-line transcript-assistant";
    currentAssistantLine.style.whiteSpace = "pre-line";
    transcriptEl.appendChild(currentAssistantLine);
    currentAssistantText = "";
  }
  currentAssistantText += deltaText;
  currentAssistantLine.textContent = currentAssistantText;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function finalizeAssistantTranscript(fullText) {
  if (currentAssistantLine) {
    currentAssistantLine.textContent = fullText; // authoritative - corrects any delta drift
    currentAssistantLine = null;
    currentAssistantText = "";
  } else {
    appendTranscript("assistant", fullText, true); // no deltas arrived - fall back to old behavior
  }
}

function appendTranscript(speaker, text, replace = false) {
  if (!transcriptEl || !text) return;
  // BUG FIX: every question+answer used to pile up in one endless
  // scrolling list. Professional voice-UI pattern is "one question at
  // a time" - replace=true wipes the box before showing the new
  // assistant line, so only the CURRENT question (plus, for a spoken
  // answer, what was heard under it) is ever visible.
  if (replace) transcriptEl.innerHTML = "";
  const line = document.createElement("div");
  line.className = `transcript-line transcript-${speaker}`;
  line.style.whiteSpace = "pre-line";
  line.textContent = text;
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// Fields where the caller might not know an edit option exists - gets a
// visible "Change" link, not just a spoken reminder (see realtime_tools.py).
// plan_action is deliberately excluded: dialogue/state_machine.py's
// jump_to() only allows editing fields in S.STAGES plus plan_choice/
// schedule_appointment, so a Change button here would just produce a
// "that field can't be edited" error - it's shown read-only instead.
const CHANGEABLE_FIELDS = new Set(Object.keys(FIELD_LABELS).filter((f) => f !== "plan_action"));

function displayValue(field, raw) {
  if (raw === undefined || raw === null || raw === "" || (Array.isArray(raw) && raw.length === 0)) return null;
  if (field === "category") return CATEGORY_LABELS[raw] || raw;
  if (field === "tonnage") return TONNAGE_LABELS[raw] || raw;
  if (field === "location") return LOCATION_LABELS[raw] || raw;
  if (field === "plan_action") return ACTION_LABELS[raw] || raw;
  if (field === "plan_choice") {
    const ids = Array.isArray(raw) ? raw : [raw];
    return ids.map((id) => lastPlans.find((p) => p.id === id)?.name || id).join(", ");
  }
  return Array.isArray(raw) ? raw.join(", ") : raw;
}

function updateReadings(slots) {
  Object.entries(FIELD_LABELS).forEach(([field]) => {
    const readingEl = document.querySelector(`.reading[data-field="${field}"]`);
    if (!readingEl) return;
    const row = readingEl.querySelector(".reading-value");
    const display = displayValue(field, slots && slots[field]);
    readingEl.classList.toggle("active", field === currentStage);
    if (row) {
      row.textContent = display || "—";
      row.classList.toggle("empty", !display);
    }
    if (CHANGEABLE_FIELDS.has(field)) {
      ensureChangeButton(readingEl, field, !!display);
    }
  });
}

function ensureChangeButton(readingEl, field, hasValue) {
  let btn = readingEl.querySelector(".reading-change-btn");
  if (!hasValue) {
    btn?.remove();
    return;
  }
  if (!btn) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reading-change-btn";
    btn.textContent = "Change";
    btn.addEventListener("click", () => {
      sendControl({ type: "edit_field", field });
      setStatus(`Say your new ${FIELD_LABELS[field].toLowerCase()}…`);
    });
    readingEl.appendChild(btn);
  }
}

// ---- connection status pill (new, additive to existing UI) ---------------

function ensureConnectionPill() {
  let pill = document.getElementById("rt-connection-pill");
  if (!pill) {
    pill = document.createElement("div");
    pill.id = "rt-connection-pill";
    pill.className = "rt-connection-pill";
    document.querySelector(".dial-wrap")?.after(pill);
  }
  return pill;
}

function setConnectionState(state) {
  // state: "connecting" | "connected" | "reconnecting" | "disconnected"
  const pill = ensureConnectionPill();
  pill.textContent = {
    connecting: "Connecting voice engine…",
    connected: "Live · S2S beta",
    reconnecting: "Reconnecting…",
    disconnected: "Disconnected",
  }[state] || state;
  pill.dataset.state = state;
}

function ensureBargeInBadge() {
  let badge = document.getElementById("rt-bargein-badge");
  if (!badge) {
    badge = document.createElement("div");
    badge.id = "rt-bargein-badge";
    badge.className = "rt-bargein-badge";
    badge.textContent = "Listening — go ahead";
    document.querySelector(".dial-wrap")?.after(badge);
  }
  return badge;
}

function showBargeIn(show) {
  ensureBargeInBadge().classList.toggle("visible", show);
}

// ---- push-to-talk fallback toggle (new, additive) -------------------------

function ensurePushToTalkToggle() {
  let toggle = document.getElementById("rt-ptt-toggle");
  if (!toggle) {
    toggle = document.createElement("button");
    toggle.id = "rt-ptt-toggle";
    toggle.type = "button";
    toggle.className = "rt-ptt-toggle";
    toggle.textContent = "Switch to push-to-talk";
    toggle.addEventListener("click", () => {
      pushToTalkMode = !pushToTalkMode;
      toggle.textContent = pushToTalkMode ? "Switch to hands-free" : "Switch to push-to-talk";
      toggle.classList.toggle("active", pushToTalkMode);
      setStatus(pushToTalkMode ? "Push-to-talk: hold the mic button to speak" : "Listening automatically");
      updateMicEnabled();
    });
    // Grouped with the connection pill / barge-in badge right under the
    // mic dial, not buried at the bottom of the page below every answered
    // field - it's a mic-behavior control, so it belongs next to the mic.
    // Same `.after(dial-wrap)` anchor those two use, so all three stack
    // together in the order they're created.
    document.querySelector(".dial-wrap")?.after(toggle);
  }
  return toggle;
}

// ---- per-stage UI: option cards, back button, typed-answer box (ported
// from the cascaded widget.js's renderStageUi/makeBackButton/makeOptionCard -
// #stage-ui existed in index.html/style.css but nothing ever populated it
// in the S2S version) ---------------------------------------------------

const stageUiEl = document.getElementById("stage-ui");

function makeBackButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "back-btn";
  btn.textContent = "Back";
  btn.setAttribute("aria-label", "Go back to the previous question");
  btn.onclick = () => sendControl({ type: "go_back" });
  return btn;
}

function makeOptionCard(opt) {
  const btn = document.createElement("button");
  if (opt.features && opt.features.length) {
    btn.className = "option-card option-card-plan";
    const [namePrice] = opt.label.split(" - $");
    const priceMatch = opt.label.match(/\$.*/);
    const name = document.createElement("div");
    name.className = "plan-card-name";
    name.textContent = namePrice;
    btn.appendChild(name);
    if (priceMatch) {
      const price = document.createElement("div");
      price.className = "plan-card-price";
      price.textContent = priceMatch[0];
      btn.appendChild(price);
    }
    const list = document.createElement("ul");
    list.className = "plan-card-features";
    opt.features.forEach((f) => {
      const li = document.createElement("li");
      li.textContent = f;
      list.appendChild(li);
    });
    btn.appendChild(list);
  } else {
    btn.className = "option-card";
    btn.textContent = opt.label;
  }
  btn.onclick = () => sendControl({ type: "select_option", value: opt.value });
  return btn;
}

function renderStageUi(ui) {
  if (!stageUiEl || !ui) return;
  stageUiEl.innerHTML = "";
  const canGoBack = currentStage && currentStage !== "full_name" && currentStage !== "closing";

  if (ui.type === "options") {
    // Track plan names/ids for the sidebar's displayValue() lookup -
    // slots.plan_choice only stores ids, not the human-readable name.
    if (currentStage === "plan_choice") {
      lastPlans = ui.options.map((o) => ({ id: o.value, name: o.label.split(" - $")[0] }));
    }
    const row = document.createElement("div");
    row.className = "options-row";
    if (canGoBack) row.appendChild(makeBackButton());
    const grid = document.createElement("div");
    grid.className = "option-grid";
    ui.options.forEach((opt) => grid.appendChild(makeOptionCard(opt)));
    row.appendChild(grid);
    stageUiEl.appendChild(row);
  } else if (ui.type === "text_input") {
    const row = document.createElement("div");
    row.className = "text-input-row";
    if (canGoBack) row.appendChild(makeBackButton());
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Type your answer";
    input.autocomplete = "off";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Send";
    const submit = () => {
      const text = input.value.trim();
      if (!text) return;
      // Previously this silently no-op'd whenever the socket wasn't open
      // (e.g. OPENAI_API_KEY missing/misconfigured server-side, or the
      // call dropped) - typing an answer and hitting Send just did
      // nothing with no explanation. Now it tells the person why.
      if (!dc || dc.readyState !== "open") {
        setStatus("Not connected - refresh the page to start a new call.");
        return;
      }
      sendControl({ type: "text_input", text });
      input.value = "";
    };
    btn.onclick = submit;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    row.appendChild(input);
    row.appendChild(btn);
    stageUiEl.appendChild(row);
  } else if (ui.type === "datetime") {
    const row = document.createElement("div");
    row.className = "options-row";
    if (canGoBack) row.appendChild(makeBackButton());
    row.appendChild(makeScheduleWidget(ui));
    stageUiEl.appendChild(row);
  }
}

// ---- schedule_appointment calendar widget ----------------------------
// Replaces the old plain typed-date text box: a month calendar + a fixed
// slot grid (business hours / slot size come from the ui payload - see
// state_machine.py's _schedule_ui, config.SCHEDULE_* on the backend).
// Picking a slot sends {type:"schedule_pick", date, time} directly -
// bypassing the model's own natural-language date parsing entirely,
// since a UI tap is already unambiguous (same reasoning as select_option).

function toISODate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function slotTimesFor(start, end, stepMinutes) {
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  const startMin = sh * 60 + sm;
  const endMin = eh * 60 + em;
  const out = [];
  for (let t = startMin; t < endMin; t += stepMinutes) {
    out.push(`${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`);
  }
  return out;
}

function formatTime12h(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  const period = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${period}`;
}

function makeScheduleWidget(ui) {
  const container = document.createElement("div");
  container.className = "schedule-widget";

  const [minY, minM, minD] = ui.min_date.split("-").map(Number);
  const [maxY, maxM] = ui.max_date.split("-").map(Number);
  let viewYear = minY;
  let viewMonth = minM - 1;
  let selectedDateStr = null;

  const calWrap = document.createElement("div");
  calWrap.className = "schedule-cal";
  const slotsWrap = document.createElement("div");
  slotsWrap.className = "schedule-slots";
  container.appendChild(calWrap);
  container.appendChild(slotsWrap);

  function renderCalendar() {
    calWrap.innerHTML = "";
    const header = document.createElement("div");
    header.className = "schedule-cal-header";

    const prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "schedule-nav";
    prevBtn.textContent = "\u2039";
    prevBtn.setAttribute("aria-label", "Previous month");
    const nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "schedule-nav";
    nextBtn.textContent = "\u203a";
    nextBtn.setAttribute("aria-label", "Next month");

    const title = document.createElement("span");
    title.className = "schedule-cal-title";
    title.textContent = new Date(viewYear, viewMonth, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });

    const atMin = viewYear === minY && viewMonth === minM - 1;
    const atMax = viewYear === maxY && viewMonth === maxM - 1;
    prevBtn.disabled = atMin;
    nextBtn.disabled = atMax;
    prevBtn.onclick = () => {
      if (prevBtn.disabled) return;
      viewMonth -= 1;
      if (viewMonth < 0) { viewMonth = 11; viewYear -= 1; }
      renderCalendar();
    };
    nextBtn.onclick = () => {
      if (nextBtn.disabled) return;
      viewMonth += 1;
      if (viewMonth > 11) { viewMonth = 0; viewYear += 1; }
      renderCalendar();
    };

    header.appendChild(prevBtn);
    header.appendChild(title);
    header.appendChild(nextBtn);
    calWrap.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "schedule-cal-grid";
    ["S", "M", "T", "W", "T", "F", "S"].forEach((d) => {
      const dow = document.createElement("span");
      dow.className = "schedule-cal-dow";
      dow.textContent = d;
      grid.appendChild(dow);
    });

    const startPad = new Date(viewYear, viewMonth, 1).getDay();
    for (let i = 0; i < startPad; i++) grid.appendChild(document.createElement("span"));

    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
    for (let d = 1; d <= daysInMonth; d++) {
      const dateStr = toISODate(new Date(viewYear, viewMonth, d));
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "schedule-cal-day";
      btn.textContent = String(d);
      const inRange = dateStr >= ui.min_date && dateStr <= ui.max_date;
      if (!inRange) {
        btn.disabled = true;
      } else {
        btn.onclick = () => { selectedDateStr = dateStr; renderCalendar(); renderSlots(); };
      }
      if (dateStr === ui.min_date) btn.classList.add("today");
      if (dateStr === selectedDateStr) btn.classList.add("selected");
      grid.appendChild(btn);
    }
    calWrap.appendChild(grid);
  }

  function renderSlots() {
    slotsWrap.innerHTML = "";
    if (!selectedDateStr) {
      const hint = document.createElement("div");
      hint.className = "schedule-slots-hint";
      hint.textContent = "Pick a date to see available times.";
      slotsWrap.appendChild(hint);
      return;
    }
    const [y, m, d] = selectedDateStr.split("-").map(Number);
    const title = document.createElement("div");
    title.className = "schedule-slots-title";
    title.textContent = new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    slotsWrap.appendChild(title);

    const grid = document.createElement("div");
    grid.className = "schedule-slots-grid";
    const bookedForDay = new Set((ui.booked && ui.booked[selectedDateStr]) || []);
    const isToday = selectedDateStr === ui.min_date;
    const now = new Date();

    slotTimesFor(ui.business_start, ui.business_end, ui.slot_minutes).forEach((time) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "schedule-slot";
      btn.textContent = formatTime12h(time);
      const [hh, mm] = time.split(":").map(Number);
      // Approximate (browser-local clock) - good enough to stop someone
      // tapping a slot that's obviously already passed today; the real
      // enforcement is server-side (is_slot_booked + the date-range check
      // in handle_schedule_appointment).
      const isPastToday = isToday && (hh < now.getHours() || (hh === now.getHours() && mm <= now.getMinutes()));
      if (bookedForDay.has(time) || isPastToday) {
        btn.disabled = true;
        btn.classList.add("schedule-slot-unavailable");
      } else {
        btn.onclick = () => {
          if (!dc || dc.readyState !== "open") {
            setStatus("Not connected - refresh the page to start a new call.");
            return;
          }
          // Lock the whole grid the moment a slot is tapped so a double
          // tap can't fire two booking attempts before the stage_update
          // that re-renders (or clears) this widget comes back.
          grid.querySelectorAll("button").forEach((b) => { b.disabled = true; });
          sendControl({ type: "schedule_pick", date: selectedDateStr, time });
        };
      }
      grid.appendChild(btn);
    });
    slotsWrap.appendChild(grid);
  }

  renderCalendar();
  renderSlots();
  return container;
}

// ---- WebRTC transport (audio browser <-> OpenAI direct) --------------------
//
// REPLACES the old manual-PCM-over-WebSocket relay (mic capture -> local VAD
// gate -> resample -> floatTo16BitPCM -> ws.send(bytes) -> backend -> OpenAI,
// and the matching decode -> AudioWorklet ring-buffer playback path coming
// back). That relay hopped audio through this backend twice (OpenAI <-> our
// Python process <-> browser, over WebSocket/TCP) instead of once - the
// extra hop, plus a hand-rolled jitter buffer standing in for what WebRTC
// already does natively, is what was producing the choppy/laggy audio
// ("Wha-t is y-ou-r em-ail"). WebRTC carries audio directly between this
// browser and OpenAI over a real-time transport built for exactly this, with
// its own adaptive jitter buffering - no relay, no manual PCM handling, no
// AudioWorklet ring buffer needed at all. See audio-worklets.js's header
// comment (now unused) for the full prior history of this bug.
//
// Business logic (state machine, tool calls, email/SMS/calendar side
// effects) stays entirely server-side, same as before - it just moves from
// "handled inline while relaying this call's audio" to "handled over a REST
// call the browser makes when the data channel tells it a tool needs
// running". Audio never touches this backend on this path.
//
// Untested against a live OpenAI connection in this sandbox (no outbound
// network access to api.openai.com here - same limitation the backend code
// already notes). Event names/shapes follow the same Realtime API docs the
// backend's own comments cite. Test end-to-end against a real call before
// relying on this for a demo.

let pc = null;           // RTCPeerConnection - carries audio directly to/from OpenAI
// AFTER
let dc = null;           // data channel "oai-events" - carries the same JSON events the old WS relay used
// dc?.send only guards against dc being null - it does NOT check readyState,
// so a send after the channel has closed (watchdog reset, late retry, call
// ending) throws an uncaught InvalidStateError and kills the tab.
function safeSend(obj) {
  if (dc && dc.readyState === "open") dc.send(JSON.stringify(obj));
}
let micTrack = null;     // local mic track, muted/unmuted in place instead of gating raw PCM
let remoteAudioEl = null;
let botAudioContext = null;
let botAudioAnalyser = null;
let botAudioSource = null;

// ---- response.create queuing, ported from realtime_session.py's
// _create_response/response.done handling. Six different triggers (tool
// calls, button clicks, typed text, push-to-talk, server VAD) can each want
// to start a response; firing response.create while one's already active
// gets rejected by OpenAI. Queue instead, same staleness rule: a queued
// entry only fires if session.stage still matches the stage it was queued
// under (see response.done handling in onRealtimeEvent below). ----
let responseActive = false;
let pendingResponses = []; // [{instructions, forStage}]
let pendingSilenceFrame = null;
let responseWatchdogTimer = null;
const handledFunctionCallIds = new Set();
const RESPONSE_WATCHDOG_MS = 2500; // inactivity window (reset on every transcript delta) - see armResponseWatchdog
const SILENCE_RMS = 0.01;
const SILENCE_HOLD_MS = 300;
const MAX_SILENCE_WAIT_MS = 3000;

// BUG FIX ("stops and doesn't say the question"): both retry paths below
// (cancelled/failed response.done, and the watchdog firing on a dropped
// response.done) used to call createResponse() with NO instructions -
// losing whatever scripted "Say exactly: ..." line was actually in
// flight, so the retry produced a blank/model-decided turn instead of
// the real question. Most likely cause of the cancellation itself: a
// noise/echo blip crossing server VAD threshold while the bot is
// mid-question - the mic track gets disabled on response.created, but a
// few already-buffered RTP frames can still land before that takes
// effect. Track what's actually in flight so retries resend the SAME
// line, and cap retries so a persistently-rejected response can't loop
// forever and compound the lag.

let activeResponseInstructions = null;
let activeResponseRetries = 0;
const MAX_RESPONSE_RETRIES = 2;
const RESPONSE_RETRY_DELAY_MS = 2000;
let didFinalFallback = false;
let lastTokenRateLimit = null; // {remaining, resetSeconds, capturedAt} - from rate_limits.updated
let turnCounter = 0; // increments each response.done with usage - for [TOKENS] logging
// Every conversation item (your voice input, its tool calls/results, its
// spoken replies) in creation order - populated from conversation.item.created
// below. Used only to trim old history (see trimConversationHistory) so
// turn count doesn't quietly slow every later turn down.
let conversationItemIds = []; // [{id, role}]
const MAX_KEPT_EXCHANGES = 2;
// Set right before we deliberately send response.cancel (UI action moving
// on early - see interruptActiveResponse) so response.done can tell "we
// cut this off on purpose, don't resend it" from a real failed/rejected
// response, which DOES need the resend-same-line retry below.
let intentionalCancel = false;

// Deletes conversation items older than the last MAX_KEPT_EXCHANGES turns.
// Only ever called after a response fully completes successfully (see
// response.done below) - never mid-response, never during a retry - so
// nothing being deleted can still be "in flight" or referenced by what's
// about to happen next. A new exchange is marked by a role:"user" item
// (that's created the moment your audio commits), which keeps a tool call
// and its result grouped with the reply they belong to - the boundary
// never lands in the middle of a pair. session.slots (backend) already
// holds the real collected data, so the model doesn't need these old
// turns in-context to know what's already been answered.
function trimConversationHistory() {
  const userIdxs = conversationItemIds
    .map((it, i) => (it.role === "user" ? i : -1))
    .filter((i) => i !== -1);
  if (userIdxs.length <= MAX_KEPT_EXCHANGES) return;
  const cutoffIdx = userIdxs[userIdxs.length - MAX_KEPT_EXCHANGES];
  const toDelete = conversationItemIds.slice(0, cutoffIdx);
  console.log(`[trim] deleting ${toDelete.length} items, ${conversationItemIds.length} -> ${conversationItemIds.length - toDelete.length}`, toDelete.map(t => t.id));
  toDelete.forEach(({ id }) => {
    safeSend({ type: "conversation.item.delete", item_id: id });
  });
  conversationItemIds = conversationItemIds.slice(cutoffIdx);
}

// AFTER
// Exponential backoff (2s, 4s...), capped hard. A live call can't sit in
// silence waiting out OpenAI's full TPM reset window (can be 60s) - the
// caller hangs up long before that. MAX_RESPONSE_RETRIES + the scripted
// fallback handle it if retries still fail after this short wait.
const RESPONSE_RETRY_DELAY_CAP_MS = 4000;
function scheduleResponseRetry(instructions, attemptNumber) {
  const delay = Math.min(
    RESPONSE_RETRY_DELAY_MS * Math.pow(2, attemptNumber - 1),
    RESPONSE_RETRY_DELAY_CAP_MS
  );
  console.warn(`[retry] attempt ${attemptNumber} in ${Math.round(delay)}ms`);
  setTimeout(() => createResponse(instructions, true), delay);
}

function armResponseWatchdog() {
  // Inactivity-based, not "whole response must finish within N seconds":
  // called again on every transcript delta below, so a long line (e.g.
  // review_summary) keeps pushing its own deadline out as it streams,
  // while a response that goes dead mid-sentence (no more deltas) still
  // gets caught quickly instead of waiting out a timer sized for the
  // longest possible line.
  if (responseWatchdogTimer) clearTimeout(responseWatchdogTimer);
  responseWatchdogTimer = setTimeout(() => {
    // AFTER
    console.warn("[watchdog] no activity within timeout - forcing reset");
    // Tell OpenAI the stalled response is actually done before asking for
    // a new one - otherwise it's often still alive server-side and the
    // new request gets rejected with conversation_already_has_active_response,
    // burning another request in the same tight window for nothing. Only if
    // one is actually active - cancelling a response that's already
    // finished/errored throws response_cancel_not_active for nothing.
    if (responseActive) safeSend({ type: "response.cancel" });
    responseActive = false;
    if (activeResponseRetries < MAX_RESPONSE_RETRIES) {
      scheduleResponseRetry(activeResponseInstructions, activeResponseRetries + 1);
    } else {
      console.error("[watchdog] gave up retrying after max attempts, moving on");
      let drained = false;
      while (pendingResponses.length) {
        const idx = pendingResponses.findIndex((p) => p.instructions !== null);
        const [entry] = pendingResponses.splice(idx >= 0 ? idx : 0, 1);
        if (entry.forStage !== currentStage) continue;
        createResponse(entry.instructions);
        drained = true;
        break;
      }
      if (!drained && activeResponseInstructions && !didFinalFallback) {
        // Normal voice turn - nothing was ever queued, so without this the
        // call goes silent here even after retries. One guarded (non-
        // looping, thanks to didFinalFallback) attempt to re-ask instead.
        didFinalFallback = true;
        console.warn("[watchdog] nothing queued - re-asking current question once more");
        scheduleResponseRetry(activeResponseInstructions, MAX_RESPONSE_RETRIES + 1);
      }
    }
  }, RESPONSE_WATCHDOG_MS);
}

// Cuts the bot off when the caller has already moved on via a UI action
// (option tap, Back, Change, schedule pick, typed answer) instead of
// letting it finish speaking a question that's no longer relevant - see
// sendControl below, where this fires before the new line is queued.
function interruptActiveResponse() {
  if (!responseActive) return;
  intentionalCancel = true;
  safeSend({ type: "response.cancel" });
}

function createResponse(instructions, _isRetry = false) {
  if (responseActive) {
    pendingResponses.push({ instructions: instructions ?? null, forStage: currentStage });
    return;
  }
  responseActive = true;
  activeResponseInstructions = instructions ?? null;
  activeResponseRetries = _isRetry ? activeResponseRetries + 1 : 0;
  if (!_isRetry) didFinalFallback = false;
  // BUG FIX (silent hang / "text not spoken"): if response.done never
  // arrives for this response (dropped data-channel message - rare, but
  // seen in testing), responseActive stayed stuck true forever, so
  // every later createResponse() call just queued behind it and NEVER
  // fired - no audio, no transcript update, no visible error. Force a
  // reset if nothing's heard back in a reasonable window and retry
  // this same response (see activeResponseInstructions above), then
  // whatever's next in the queue.
  armResponseWatchdog();
  const payload = { type: "response.create" };
  if (instructions) {
    // Scripted "say exactly" lines: lock tool_choice off and give empty
    // input so these turns don't reprocess (and re-bill/re-latency) the
    // whole conversation - same reasoning as the backend's _create_response.
    payload.response = { instructions, tool_choice: "none", input: [] };
  }
  if (currentTools) {
    // Per-turn tools, same reasoning as instructions above: this is what
    // used to be a session.update on every stage change (cache-resetting).
    // Harmless to include alongside tool_choice:"none" above - it's just
    // ignored on scripted turns that can't call a function anyway.
    payload.response = payload.response || {};
    payload.response.tools = currentTools;
  }
  // BUG FIX: mute on SEND, not on the response.created echo. Waiting for
  // response.created (a round trip to OpenAI and back) left a window
  // where the mic track was still live right as the bot's own audio
  // started - any echo/noise in that gap could cross server VAD and
  // auto-cancel the response before it finished, which is the most
  // likely trigger for the cancelled-response retries above.
  cancelPendingMicUnmute();
  micMuted = true;
  updateMicEnabled();
  safeSend(payload);
}

function injectUserNote(text) {
  // Records a non-spoken UI action (tap/Back/Change) as an ordinary user
  // turn so the model's own conversation history stays coherent, without
  // asking for a response - see realtime_session.py's _inject_user_note
  // for the full "why" (this is the fix for clicks derailing the call).
  safeSend({
    type: "conversation.item.create",
    item: { type: "message", role: "user", content: [{ type: "input_text", text }] },
  });
}

// ---- tool-call dispatch (REST round-trip to the backend, audio-free) ------

async function callTool(name, args, _attempt = 0) {
  const MAX_TOOL_CALL_RETRIES = 2;
  try {
    const resp = await fetch(`${API_BASE_URL}/api/session/${sessionId}/tool-call`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(API_KEY ? { "X-API-Key": API_KEY } : {}) },
      body: JSON.stringify({ name, arguments: args }),
    });
    if ([502, 503, 504].includes(resp.status) && _attempt < MAX_TOOL_CALL_RETRIES) {
      console.warn(`[tool-call] ${resp.status} from tunnel, retrying (attempt ${_attempt + 1})`);
      await new Promise((r) => setTimeout(r, 500 * (_attempt + 1)));
      return callTool(name, args, _attempt + 1);
    }
    let result;
    try {
      result = await resp.json();
    } catch {
      // Tunnel error page ("Bad Gateway" etc.) isn't JSON - don't let this
      // throw past the retries above into a generic uncaught-looking failure.
      result = { ok: false, detail: `Voice engine error (status ${resp.status})` };
    }
    console.log("[T3] tool-call response", performance.now());   // ADD THIS
    if (!resp.ok) {
      setStatus(result.detail || "Voice engine error");
      return result;
    }
    currentStage = result.stage;
    updateReadings(result.slots);
    renderStageUi(result.ui);
    if (result.tools) {
      // Stage moved - stash the new stage's tools; the NEXT response.create
      // (any of them - scripted say_next, voice turn, text input, retry)
      // carries them as a per-turn param instead of mutating session-level
      // tools, which is what was resetting the prompt cache on every stage
      // change (see createResponse).
      currentTools = result.tools;
    }
    if (result.stage === "closing") scheduleCallEnd("completed");
    return result;
  } catch (err) {
    console.error("tool-call failed", err);
    setStatus("Voice engine error");
    return { ok: false };
  }
}

// Shared response after any UI-driven tool call (sendControl below) -
// mirrors handleFunctionCall's success/failure handling so a rejected
// call (e.g. stale currentStage) never goes silent.
function respondToToolResult(r) {
  interruptActiveResponse();
  if (!r.ok) console.warn("[tool] rejected", r.error);
  createResponse(r.say_next ? `Say exactly, word for word: ${r.say_next}` : undefined);
}

// Model-driven function call, arrived via response.function_call_arguments.done.
async function handleFunctionCall(event) {
  if (event.call_id && handledFunctionCallIds.has(event.call_id)) return;
  if (event.call_id) handledFunctionCallIds.add(event.call_id);
  let args = {};
  try { args = JSON.parse(event.arguments || "{}"); } catch { /* leave {} */ }


  const result = await callTool(event.name, args);
  safeSend({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: event.call_id, output: JSON.stringify(result) },
  });
  if (!result.ok) console.warn(`[tool] rejected ${event.name} call`, result.error);
  createResponse(result.say_next ? `Say exactly, word for word: ${result.say_next}` : undefined);
}

// UI-driven actions (option tap, Back, Change, schedule pick, typed text,
// push-to-talk release) - the button/typed-input call sites earlier in this
// file all call this instead of touching the connection directly, mirroring
// what _handle_client_control used to do server-side per message type.
function sendControl(msg) {
  interruptActiveResponse();
  if (msg.type === "go_back") {
    injectUserNote("[System note: caller tapped Back - already handled, no tool call needed.]");
    callTool("go_back_or_edit", { action: "go_back" }).then(respondToToolResult);
  } else if (msg.type === "select_option") {
    const field = currentStage;
    injectUserNote(`[System note: caller tapped an option on screen for ${field} - already saved, no tool call needed for this.]`);
    callTool("confirm_slot", { field, value: msg.value }).then(respondToToolResult);
  } else if (msg.type === "edit_field") {
    injectUserNote(`[System note: caller tapped Change next to ${msg.field} - already handled, no tool call needed.]`);
    callTool("go_back_or_edit", { action: "edit_field", field: msg.field }).then(respondToToolResult);
  } else if (msg.type === "schedule_pick") {
    injectUserNote(`[System note: caller picked ${msg.date} at ${msg.time} on the calendar widget - already handled, no tool call needed.]`);
    callTool("schedule_appointment", { call_date: msg.date, call_time: msg.time }).then(respondToToolResult);
  } else if (msg.type === "replay") {
    callTool("go_back_or_edit", { action: "repeat" }).then(respondToToolResult);
  } else if (msg.type === "text_input") {
    const text = (msg.text || "").trim();
    if (!text) return;
    safeSend({
      type: "conversation.item.create",
      item: { type: "message", role: "user", content: [{ type: "input_text", text }] },
    });
    createResponse();
  } else if (msg.type === "push_to_talk_commit") {
    // No byte counter to check anymore (audio streams continuously over
    // WebRTC, not as chunks we see) - an empty-buffer commit just comes
    // back as input_audio_buffer_commit_empty, already handled as a no-op
    // in onRealtimeEvent below, same as the old path.
    safeSend({ type: "input_audio_buffer.commit" });
    createResponse();
  }
}

// ---- mic mute (echo suppression while the bot is speaking) ----------------
// Old path muted by gating whether captured PCM chunks got sent. With a
// live WebRTC track there's a simpler native equivalent: disable the track
// itself. Tied to response.created/response.done instead of a local
// playback-queue-empty message, since WebRTC audio no longer passes through
// any queue this code can see.
function updateMicEnabled() {
  if (!micTrack) return;
  micTrack.enabled = !micMuted && (!pushToTalkMode || pushToTalkActive);
}

function cancelPendingMicUnmute() {
  if (pendingSilenceFrame !== null) {
    cancelAnimationFrame(pendingSilenceFrame);
    pendingSilenceFrame = null;
  }
}

function waitForBotSilenceThenUnmute() {
  cancelPendingMicUnmute();
  const samples = botAudioAnalyser ? new Float32Array(botAudioAnalyser.fftSize) : null;
  const startedAt = performance.now();
  let silentSince = null;

  const tick = () => {
    const now = performance.now();
    let rms = Infinity;
    if (samples) {
      botAudioAnalyser.getFloatTimeDomainData(samples);
      rms = Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length);
    }

    if (rms < SILENCE_RMS) {
      silentSince ??= now;
      if (now - silentSince >= SILENCE_HOLD_MS || now - startedAt >= MAX_SILENCE_WAIT_MS) {
        pendingSilenceFrame = null;
        micMuted = false;
        updateMicEnabled();
        micBtn?.classList.remove("speaking");
        return;
      }
    } else {
      silentSince = null;
      if (now - startedAt >= MAX_SILENCE_WAIT_MS) {
        pendingSilenceFrame = null;
        micMuted = false;
        updateMicEnabled();
        micBtn?.classList.remove("speaking");
        return;
      }
    }
    pendingSilenceFrame = requestAnimationFrame(tick);
  };

  pendingSilenceFrame = requestAnimationFrame(tick);
}

// ---- barge-in blip filter ---------------------------------------------------
// Ported from realtime_session.py's speech_stopped handler: a noise/echo
// blip that barely crosses VAD still opens a turn, and forcing the model to
// answer near-silent audio is what produced hallucinated replies. The
// backend judged this from raw bytes streamed since speech_started (~290ms
// worth); we no longer see raw bytes client-side, so use elapsed wall time
// since speech_started as the equivalent threshold.
const BLIP_THRESHOLD_MS = 290;
let speechStartedAt = null;
let ignoredBlipCount = 0;
let ignoringMutedInput = false;

// ---- call-ending timers, ported from RealtimeSession._watchdog -----------
const IDLE_TIMEOUT_MS = 120000;
const MAX_CALL_MS = 300000;
const CALL_END_GRACE_MS = 2000;
let lastActivityAt = 0;
let callEndTimer = null;
let idleCheckInterval = null;
let callStartedAt = 0;

function noteActivity() { lastActivityAt = performance.now(); }

function scheduleCallEnd(reason) {
  if (callEndTimer) return;
  callEndTimer = setTimeout(() => endCall(reason), CALL_END_GRACE_MS);
}

function endCall(reason) {
  intentionalClose = true;
  setConnectionState("disconnected");
  setStatus(
    reason === "idle" ? "Call ended (no activity). Refresh to start a new call." :
    reason === "max_duration" ? "Call ended (time limit reached). Refresh to start a new call." :
    "Call complete. Refresh to start a new call."
  );
  if (idleCheckInterval) { clearInterval(idleCheckInterval); idleCheckInterval = null; }
  cancelPendingMicUnmute();
  if (micTrack) { micTrack.stop(); micTrack = null; }
  dc?.close();
  pc?.close();
}

// ---- Realtime protocol events over the data channel ------------------------
// Same event names/shapes as the old server-relayed WebSocket used (WebRTC's
// data channel carries the identical Realtime API JSON events) - only NOTE:
// response.output_audio.delta does NOT need handling here. Audio arrives via
// the actual WebRTC media track (pc.ontrack below), not as base64 JSON
// deltas on the data channel - that's the whole fix.
function onRealtimeEvent(event) {
  noteActivity();
  switch (event.type) {
    case "input_audio_buffer.speech_started":
      if (micMuted || responseActive) {
        ignoringMutedInput = true;
        showBargeIn(false);
        break;
      }
      ignoringMutedInput = false;
      speechStartedAt = performance.now();
      showBargeIn(true);
      micBtn?.classList.remove("speaking");
      break;

    case "input_audio_buffer.speech_stopped": {
      showBargeIn(false);
      console.log("[T1] speech_stopped", performance.now());   // ADD THIS
      if (ignoringMutedInput || micMuted || responseActive) {
        ignoringMutedInput = false;
        ignoredBlipCount += 1;
        break;
      }
      const elapsed = speechStartedAt ? performance.now() - speechStartedAt : 0;
      if (elapsed < BLIP_THRESHOLD_MS) {
        ignoredBlipCount += 1; // matching aux transcript below will be dropped too
        break;
      }
      // Real spoken turn queues behind any in-flight bot response instead
      // of cutting it off - see createResponse's queueing.
      createResponse();
      break;
    }

    case "conversation.item.created":
    case "conversation.item.added": {
      if (event.item?.id && !conversationItemIds.some((it) => it.id === event.item.id)) {
        conversationItemIds.push({ id: event.item.id, role: event.item.role || event.item.type });
      }
      break;
    }

    case "conversation.item.input_audio_transcription.completed": {
      const transcript = event.transcript || "";
      if (ignoredBlipCount > 0) { ignoredBlipCount -= 1; break; }
      // if (transcript) appendTranscript("you", transcript);
      break;
    }

    case "response.output_audio_transcript.delta":
      if (event.delta) {
        if (!firstDeltaLogged) {   // ADD: declare `let firstDeltaLogged = false;` near top-level state, reset it to false right where you do `responseActive = true;` in the response.created case (line 949)
          console.log("[T4] first audio delta", performance.now());
          firstDeltaLogged = true;
        }
        appendTranscriptDelta(event.delta);
        armResponseWatchdog();
      }
      break;

    case "response.output_audio_transcript.done":
      if (event.transcript) finalizeAssistantTranscript(event.transcript);
      break;

    case "response.function_call_arguments.done":
      console.log("[T2] function_call_arguments.done", performance.now());   // ADD THIS
      handleFunctionCall(event);
      break;

    case "response.created":
      responseActive = true;
      firstDeltaLogged = false;   // ADD THIS
      cancelPendingMicUnmute();
      micMuted = true; // bot is about to speak - don't stream mic (echo) back in
      updateMicEnabled();
      break;

    case "response.done": {
      if (event.response?.usage) {
        const u = event.response.usage;
        turnCounter++;
        console.log(
          `[TOKENS] turn=${turnCounter} total=${u.total_tokens} ` +
          `in=${u.input_tokens}(cached=${u.input_token_details?.cached_tokens ?? 0},` +
          `text=${u.input_token_details?.text_tokens ?? 0},audio=${u.input_token_details?.audio_tokens ?? 0}) ` +
          `out=${u.output_tokens}(text=${u.output_token_details?.text_tokens ?? 0},audio=${u.output_token_details?.audio_tokens ?? 0})`
        );
        fetch(`${API_BASE_URL}/api/session/${sessionId}/log-usage`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
          },
          body: JSON.stringify(event.response.usage),
        }).catch((err) => console.warn("[usage] failed to report usage", err));
      }
      const status = event.response?.status;
      const wasIntentionalCancel = intentionalCancel;
      intentionalCancel = false;
      if (status === "cancelled" && wasIntentionalCancel) {
        // Caller already moved on (UI action) - the cut-off line is stale,
        // don't resend it. Fall straight through to whatever's queued.
        responseActive = false;
        if (responseWatchdogTimer) { clearTimeout(responseWatchdogTimer); responseWatchdogTimer = null; }
        waitForBotSilenceThenUnmute();
        while (pendingResponses.length) {
          const idx = pendingResponses.findIndex((p) => p.instructions !== null);
          const [entry] = pendingResponses.splice(idx >= 0 ? idx : 0, 1);
          if (entry.forStage !== currentStage) continue;
          createResponse(entry.instructions);
          break;
        }
        break;
      }
      responseActive = false;
      if (responseWatchdogTimer) { clearTimeout(responseWatchdogTimer); responseWatchdogTimer = null; }
      // Trim regardless of success or failure - a failed/rate-limited
      // response used to skip this entirely, so every retry after it
      // (and every turn after that) kept resending a bigger, never-pruned
      // conversation straight into the same rate limit that just rejected
      // it. That's a self-inflicted feedback loop, not bad luck.
      trimConversationHistory();
      if (status && status !== "completed") {
        console.warn(`[response.done] status=${status} - retrying (attempt ${activeResponseRetries + 1})`, event.response?.status_details);
        if (activeResponseRetries < MAX_RESPONSE_RETRIES) {
          scheduleResponseRetry(activeResponseInstructions, activeResponseRetries + 1);
        } else {
          console.error("[response.done] gave up retrying after max attempts, moving on");
          let drained = false;
          while (pendingResponses.length) {
            const idx = pendingResponses.findIndex((p) => p.instructions !== null);
            const [entry] = pendingResponses.splice(idx >= 0 ? idx : 0, 1);
            if (entry.forStage !== currentStage) continue;
            createResponse(entry.instructions);
            drained = true;
            break;
          }
          if (!drained && activeResponseInstructions && !didFinalFallback) {
            didFinalFallback = true;
            console.warn("[response.done] nothing queued - re-asking current question once more");
            scheduleResponseRetry(activeResponseInstructions, MAX_RESPONSE_RETRIES + 1);
          }
        }
        break;
      }
      // response.done means generation ended, not that the WebRTC playout
      // buffer is empty. Keep the mic muted until the received audio is
      // actually silent, so the tail cannot become the next caller turn.
      waitForBotSilenceThenUnmute();
      while (pendingResponses.length) {
        const idx = pendingResponses.findIndex((p) => p.instructions !== null);
        const [entry] = pendingResponses.splice(idx >= 0 ? idx : 0, 1);
        if (entry.forStage !== currentStage) continue; // stale - dropped, matches backend behavior
        createResponse(entry.instructions);
        break;
      }
      break;
    }

    case "rate_limits.updated":
      event.rate_limits?.forEach((rl) => {
        if (rl.name === "tokens") {
          lastTokenRateLimit = { remaining: rl.remaining, resetSeconds: rl.reset_seconds, capturedAt: performance.now() };
          console.log(`[TPM] remaining=${rl.remaining} resetIn=${rl.reset_seconds}s`);
        }
      });
      break;
    case "error":
      if (event.error?.code === "input_audio_buffer_commit_empty") {
        responseActive = false; // push-to-talk released with nothing captured - not a real error
        break;
      }
      console.error("realtime API error", event);
      responseActive = false;
      setStatus("Voice engine error");
      break;

    default:
      break;
  }
}

// ---- session + connection lifecycle ----------------------------------------

async function startSession() {
  setConnectionState("connecting");
  const existingId = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (existingId) {
    setStatus("Resuming your call…");
    try {
      const resp = await fetch(`${API_BASE_URL}/api/session/${existingId}`, {
        headers: API_KEY ? { "X-API-Key": API_KEY } : {},
      });
      if (resp.ok) {
        const data = await resp.json();
        sessionId = data.session_id;
        appendTranscript("assistant", `(Resumed) ${data.assistant_text}`, true);
        currentStage = data.stage;
        updateReadings(data.slots);
        renderStageUi(data.ui);
        await connectWebRTC();
        return;
      }
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } catch (e) {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    }
  }

  setStatus("Starting session…");
  const resp = await fetch(`${API_BASE_URL}/api/session/start`, {
    method: "POST",
    headers: API_KEY ? { "X-API-Key": API_KEY } : {},
  });
  const data = await resp.json();
  sessionId = data.session_id;
  sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  appendTranscript("assistant", data.assistant_text, true);
  currentStage = data.stage;
  updateReadings(data.slots);
  renderStageUi(data.ui);
  await connectWebRTC();
}

async function connectWebRTC() {
  const authHeaders = API_KEY ? { "X-API-Key": API_KEY } : {};

  const [tokenResp, configResp] = await Promise.all([
    fetch(`${API_BASE_URL}/api/session/${sessionId}/realtime-token`, { method: "POST", headers: authHeaders }),
    fetch(`${API_BASE_URL}/api/session/${sessionId}/realtime-session-config`, { headers: authHeaders }),
  ]);
  if (!tokenResp.ok || !configResp.ok) {
    setStatus("Could not start the voice engine.");
    return;
  }
  const tokenData = await tokenResp.json();
  const sessionConfig = await configResp.json();
  const ephemeralKey = tokenData.client_secret?.value || tokenData.value;
  if (!ephemeralKey) {
    setStatus("Could not start the voice engine.");
    return;
  }

  const micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  micTrack = micStream.getAudioTracks()[0];
  micTrack.enabled = false; // mute BEFORE addTrack - see micMuted's declaration for why

  pc = new RTCPeerConnection();
  pc.addTrack(micTrack, micStream);

  if (!remoteAudioEl) {
    remoteAudioEl = document.createElement("audio");
    remoteAudioEl.autoplay = true;
    document.body.appendChild(remoteAudioEl);
  }
  pc.ontrack = (event) => {
    remoteAudioEl.srcObject = event.streams[0];
    botAudioContext ??= new AudioContext();
    botAudioContext.resume().catch(() => {});
    botAudioSource?.disconnect();
    botAudioSource = botAudioContext.createMediaStreamSource(event.streams[0]);
    botAudioAnalyser = botAudioContext.createAnalyser();
    botAudioAnalyser.fftSize = 512;
    botAudioSource.connect(botAudioAnalyser);
  };

  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
      if (!intentionalClose) attemptReconnect();
    }
  };

  dc = pc.createDataChannel("oai-events");
  dc.onopen = () => {
    reconnectAttempts = 0;
    lastActivityAt = performance.now();
    callStartedAt = performance.now();
    safeSend({ type: "session.update", session: sessionConfig.session });
    createResponse(sessionConfig.greeting_instructions);
    setConnectionState("connected");
    setStatus("Connected — say hello");
    micBtn?.classList.add("listening");
    updateMicEnabled();
    if (!idleCheckInterval) {
      idleCheckInterval = setInterval(() => {
        const now = performance.now();
        if (now - callStartedAt >= MAX_CALL_MS) { scheduleCallEnd("max_duration"); return; }
        if (now - lastActivityAt >= IDLE_TIMEOUT_MS) { scheduleCallEnd("idle"); }
      }, 1000);
    }
  };
  dc.onmessage = (event) => {
    try { onRealtimeEvent(JSON.parse(event.data)); }
    catch (e) { console.error("event parse error", e); }
  };
  dc.onerror = (err) => console.error("data channel error", err);

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const sdpResponse = await fetch(`https://api.openai.com/v1/realtime/calls?model=${sessionConfig.session.model}`, {
    method: "POST",
    body: offer.sdp,
    headers: { Authorization: `Bearer ${ephemeralKey}`, "Content-Type": "application/sdp" },
  });
  if (!sdpResponse.ok) {
    setStatus("Could not connect to the voice engine.");
    return;
  }
  const answerSdp = await sdpResponse.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
}

function attemptReconnect() {
  if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    setConnectionState("disconnected");
    setStatus("Lost connection to the voice engine. Refresh to start a new call.");
    return;
  }
  reconnectAttempts += 1;
  setConnectionState("reconnecting");
  setStatus(`Reconnecting… (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
  const delay = RECONNECT_BASE_DELAY_MS * Math.pow(2, reconnectAttempts - 1);
  setTimeout(() => {
    pc?.close();
    connectWebRTC().catch((err) => {
      console.error(err);
      attemptReconnect();
    });
  }, delay);
}

// Push-to-talk: hold mic button to gate input manually when server VAD misfires.
micBtn?.addEventListener("mousedown", () => { if (pushToTalkMode) { pushToTalkActive = true; updateMicEnabled(); } });
micBtn?.addEventListener("touchstart", () => { if (pushToTalkMode) { pushToTalkActive = true; updateMicEnabled(); } });
["mouseup", "mouseleave", "touchend"].forEach((evt) =>
  micBtn?.addEventListener(evt, () => {
    if (pushToTalkMode && pushToTalkActive) {
      pushToTalkActive = false;
      updateMicEnabled();
      sendControl({ type: "push_to_talk_commit" });
    }
  })
);

document.getElementById("replay-btn")?.addEventListener("click", () => {
  sendControl({ type: "replay" });
});

document.getElementById("new-session-btn")?.addEventListener("click", () => {
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
  intentionalClose = true;
  dc?.close();
  pc?.close();
  window.location.reload();
});

window.addEventListener("beforeunload", () => {
  // A reload/close mid-call left the old session's WebRTC connection
  // live with nothing to close it - the backend only noticed via its
  // own idle timeout, minutes later. If the page reloads and resumes
  // this same sessionId in the meantime, you get two live sessions
  // burning tokens against the same account at once (see the 12:22:36
  // vs 12:22:41 double getUserMedia). Tear down explicitly on unload
  // instead - sessionStorage stays intact so resume still works, it
  // just won't be resuming into a still-live connection anymore.
  intentionalClose = true;
  safeSend({ type: "response.cancel" });
  dc?.close();
  pc?.close();
});

window.addEventListener("DOMContentLoaded", () => {
  ensurePushToTalkToggle();
  startSession().catch((err) => {
    console.error(err);
    setStatus("Could not start voice session.");
  });
});