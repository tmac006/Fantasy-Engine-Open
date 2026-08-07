// MAIN-world content script, document_start, on ESPN draft room pages.
// Wraps window.WebSocket before ESPN's client connects and passively mirrors
// draft frames to the isolated-world bridge via postMessage. Read-only:
// nothing is ever sent on the socket.

const TAP_MESSAGE_TYPE = "fantasy-draft-tap";
const DRAFT_HOST = "fantasydraft";

const NativeWebSocket = window.WebSocket;

function mirror(payload: unknown): void {
  window.postMessage({ type: TAP_MESSAGE_TYPE, payload }, window.location.origin);
}

class TappedWebSocket extends NativeWebSocket {
  constructor(url: string | URL, protocols?: string | string[]) {
    super(url, protocols);
    const target = String(url);
    if (target.includes(DRAFT_HOST)) {
      this.addEventListener("open", () => mirror({ event: "open", url: target }));
      this.addEventListener("message", (ev: MessageEvent) => {
        if (typeof ev.data === "string") mirror({ event: "frame", data: ev.data });
      });
      this.addEventListener("close", () => mirror({ event: "close" }));
      this.addEventListener("error", () => mirror({ event: "error" }));
    }
  }
}

window.WebSocket = TappedWebSocket as typeof WebSocket;
