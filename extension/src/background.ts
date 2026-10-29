// Service worker: the panel does all the work; this only wires it to the
// toolbar and lets the ESPN content scripts write to session storage.

// Registered FIRST and deliberately. The ESPN bridge cannot write any draft
// state until this answers, so if anything above it threw the whole tool would
// sit blank for a draft. Nothing here may depend on a later statement
// succeeding.
chrome.runtime.onMessage.addListener((message: unknown, sender, sendResponse) => {
  if ((message as { type?: string })?.type === "get-tab-id") {
    // Content scripts cannot see their own tab id, but the bridge needs it to
    // key its session-storage slot per draft-room tab (two open rooms must
    // never share a slot). Echo it back from the sender.
    sendResponse({ tabId: sender.tab?.id ?? null });
  }
  // Synchronous reply: returning undefined closes the channel after this
  // listener returns, by which point sendResponse has already delivered.
  return undefined;
});

chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

// Lets the isolated content script write to chrome.storage.session. Guarded
// because an older Chrome without setAccessLevel would throw synchronously and
// take the rest of this file's registrations down with it.
try {
  void chrome.storage.session.setAccessLevel({
    accessLevel: "TRUSTED_AND_UNTRUSTED_CONTEXTS",
  });
} catch (error) {
  console.warn("draft-assistant: could not widen session storage access", error);
}
