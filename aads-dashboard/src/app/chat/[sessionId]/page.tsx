"use client";
// Path-based session URL → hash-based redirect
// Handles: /chat/<uuid>  →  /chat#<uuid>
// This allows direct-linking to a specific AADS chat session.
import { useEffect } from "react";
import { useParams } from "next/navigation";

export default function ChatSessionRedirect() {
  const params = useParams();
  const sessionId = typeof params?.sessionId === "string" ? params.sessionId : "";

  useEffect(() => {
    if (!sessionId) {
      window.location.replace("/chat");
      return;
    }
    const token =
      typeof window !== "undefined"
        ? localStorage.getItem("aads_token") || ""
        : "";
    if (!token) {
      const next = encodeURIComponent(`/chat#${sessionId}`);
      window.location.replace(`/login?next=${next}&reason=session_required`);
    } else {
      window.location.replace(`/chat#${sessionId}`);
    }
  }, [sessionId]);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        fontFamily: "sans-serif",
        color: "#6b7280",
        fontSize: "14px",
      }}
    >
      세션을 불러오는 중…
    </div>
  );
}
