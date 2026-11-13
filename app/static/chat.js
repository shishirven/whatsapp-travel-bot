const assistantName = window.ASSISTANT_NAME || "Assistant";
const thread = document.getElementById("thread");
const composer = document.getElementById("composer");
const messageInput = document.getElementById("message");
const typing = document.getElementById("typing");
const chatTitle = document.getElementById("chat-title");
const waId = document.getElementById("wa-id");
const scoreEl = document.getElementById("score");
const scoreReason = document.getElementById("score-reason");
const handoffEl = document.getElementById("handoff");

let conversationId = null;
let messages = [];
let busy = false;

document.querySelectorAll(".ad-card").forEach((button) => {
  button.addEventListener("click", () => startChat(button));
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage();
});

async function startChat(button) {
  if (busy) return;
  busy = true;
  document.querySelectorAll(".ad-card").forEach((card) => card.classList.remove("selected"));
  button.classList.add("selected");
  const profile = document.getElementById("profile-name").value.trim();
  const adId = button.dataset.adId || null;
  try {
    const data = await postJson("/api/conversations", {
      ad_id: adId,
      profile_name: profile || null,
    });
    conversationId = data.conversation_id;
    messages = data.messages;
    renderThread(messages);
    renderLead(data);
    composer.hidden = false;
    messageInput.focus();
  } catch (error) {
    showError(error.message);
  } finally {
    busy = false;
  }
}

async function sendMessage() {
  const text = messageInput.value.trim();
  if (!text || !conversationId || busy) return;
  busy = true;
  messageInput.value = "";
  messageInput.disabled = true;
  composer.querySelector("button").disabled = true;
  messages = messages.concat([{ direction: "in", body: text, created_at: "" }]);
  renderThread(messages);
  typing.hidden = false;
  try {
    const data = await postJson("/api/chat", { conversation_id: conversationId, text });
    messages = data.messages;
    renderThread(messages);
    renderLead(data);
  } catch (error) {
    showError(error.message);
  } finally {
    typing.hidden = true;
    messageInput.disabled = false;
    composer.querySelector("button").disabled = false;
    messageInput.focus();
    busy = false;
  }
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    data = null;
  }
  if (!response.ok) {
    throw new Error(errorMessage(data));
  }
  return data;
}

function renderThread(items) {
  thread.replaceChildren();
  if (!items.length) return;
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "bubble " + (item.direction === "out" ? "out" : "in");
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = item.direction === "out" ? assistantName : "Customer";
    const text = document.createElement("p");
    text.textContent = item.body;
    row.append(who, text);
    if (item.created_at) {
      const time = document.createElement("time");
      time.textContent = item.created_at;
      row.append(time);
    }
    thread.append(row);
  }
  thread.scrollTop = thread.scrollHeight;
}

function renderLead(data) {
  const lead = data.lead;
  const ad = data.ad;
  chatTitle.textContent = ad ? ad.headline : "Organic chat";
  waId.textContent = data.wa_id ? "WhatsApp id " + data.wa_id : "";
  scoreEl.textContent = lead.score;
  scoreEl.className = "score " + lead.score;
  scoreReason.textContent = lead.score_reason || "";
  setFact("job_type", lead.job_label || lead.job_type);
  setFact("suburb", lead.suburb);
  setFact("urgency", lead.urgency);
  setFact("description", lead.description);
  setFact("preferred_time", lead.preferred_time);
  setFact("contact_name", lead.contact_name);
  const quoteNote = document.getElementById("quote-note");
  if (lead.quote && lead.quote.min_aud != null) {
    quoteNote.hidden = false;
    quoteNote.textContent =
      "Internal draft " + lead.quote.min_aud + "–" + lead.quote.max_aud +
      " (" + lead.quote.status + "). Not sent to the customer.";
  } else {
    quoteNote.hidden = true;
  }
  const bookingNote = document.getElementById("booking-note");
  if (lead.booking && lead.booking.status === "booked") {
    bookingNote.hidden = false;
    bookingNote.textContent = "Booked " + (lead.booking.label || "");
  } else if (lead.booking && lead.booking.status === "offered") {
    bookingNote.hidden = false;
    bookingNote.textContent = "Visit times offered, not booked yet.";
  } else {
    bookingNote.hidden = true;
  }
  handoffEl.hidden = !lead.handoff;
  handoffEl.textContent = lead.handoff_reason === "human_requested"
    ? "Asked for a person — with the electrician"
    : "Handed to the electrician";
}

function setFact(name, value) {
  const node = document.getElementById("f-" + name);
  node.textContent = value == null || value === "" ? "—" : String(value);
}

function showError(message) {
  const note = document.createElement("p");
  note.className = "error";
  note.textContent = message;
  thread.append(note);
}

function errorMessage(data) {
  if (!data || data.detail == null) return "Something went wrong";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => item.msg || "Invalid request").join(" ");
  }
  return "Something went wrong";
}
