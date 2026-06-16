"use client";

type WSMessageHandler = (message: any) => void;

class WebSocketClient {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: Map<string, Set<WSMessageHandler>> = new Map();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = 2000;

  constructor() {
    const protocol = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = typeof window !== "undefined" ? window.location.host : "localhost";
    this.url = `${protocol}//${host}/ws`;
  }

  connect(): void {
    if (typeof window === "undefined") return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        console.log("[WS] Connected");
        this.reconnectDelay = 2000;
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

      this.ws.onclose = () => {
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
