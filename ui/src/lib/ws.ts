"use client";

import { api } from "@/lib/api";

type WSMessageHandler = (message: any) => void;

// Server close code for "no valid viewer session" (backend/app/interface/api/ws.py).
const WS_UNAUTHORIZED = 4401;

class WebSocketClient {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: Map<string, Set<WSMessageHandler>> = new Map();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = 2000;
  // Set after the server refused the session once and a cookie renewal was tried; a
  // second refusal stops reconnecting until connect() is called again (next subscribe).
  private sessionRenewed = false;
  private stopped = false;

  constructor() {
    const protocol = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = typeof window !== "undefined" ? window.location.host : "103.93.216.37";
    this.url = `${protocol}//${host}/ws`;
  }

  connect(): void {
    if (typeof window === "undefined") return;
    this.stopped = false;
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;

    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        console.log("[WS] Connected");
        this.reconnectDelay = 2000;
        this.sessionRenewed = false;
      };

      this.ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          const type = message.type;
          if (type && this.handlers.has(type)) {
            this.handlers.get(type)!.forEach((handler) => handler(message));
          }
          // Also notify "all" subscribers
          if (this.handlers.has("*")) {
            this.handlers.get("*")!.forEach((handler) => handler(message));
          }
        } catch (e) {
          console.warn("[WS] Failed to parse message", e);
        }
      };

      this.ws.onclose = (event) => {
        // Ignore a late close from a socket that has already been replaced.
        if (this.ws !== event.target) return;
        this.ws = null;
        if (this.stopped || this.handlers.size === 0) return;
        if (event.code === WS_UNAUTHORIZED) {
          if (this.sessionRenewed) {
            console.warn("[WS] No valid session; realtime updates paused");
            this.stopped = true;
            return;
          }
          // The viewer cookie expired or is missing — reissue it once, then retry.
          this.sessionRenewed = true;
          api.auth
            .refreshViewerSession()
            .then(() => this.scheduleReconnect())
            .catch(() => { this.stopped = true; });
          return;
        }
        this.sessionRenewed = false;
        console.log("[WS] Disconnected, reconnecting...");
        this.scheduleReconnect();
      };

      this.ws.onerror = () => {
        this.ws?.close();
      };
    } catch (e) {
      this.scheduleReconnect();
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, 30000);
      this.connect();
    }, this.reconnectDelay);
  }

  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  subscribe(type: string, handler: WSMessageHandler): () => void {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, new Set());
    }
    this.handlers.get(type)!.add(handler);

    // Auto-connect on first subscribe
    this.connect();

    // Return unsubscribe function
    return () => {
      this.handlers.get(type)?.delete(handler);
      if (this.handlers.get(type)?.size === 0) {
        this.handlers.delete(type);
      }
      // Nobody listening any more (e.g. after logout) — close instead of reconnecting.
      if (this.handlers.size === 0) this.disconnect();
    };
  }

  send(message: any): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }
}

// Singleton instance
let wsClient: WebSocketClient | null = null;

export function getWSClient(): WebSocketClient {
  if (!wsClient) {
    wsClient = new WebSocketClient();
  }
  return wsClient;
}

export function useWSSubscription(type: string, handler: WSMessageHandler): void {
  if (typeof window === "undefined") return;

  const { useEffect } = require("react");
  useEffect(() => {
    const client = getWSClient();
    const unsubscribe = client.subscribe(type, handler);
    return unsubscribe;
  }, [type]);
}
