"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { logout } from "@/lib/auth";
import { api } from "@/lib/api";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1";

export default function Header({ title }: { title: string }) {
  const [health, setHealth] = useState<string>("checking...");
  const [pipelineStatus, setPipelineStatus] = useState<string>("UNKNOWN");
  const router = useRouter();
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    api.getHealth()
      .then(() => setHealth("API OK"))
      .catch(() => setHealth("API offline"));
  }, []);

  useEffect(() => {
    const es = new EventSource(`${BASE_URL}/ops/stream`);
    esRef.current = es;

    es.addEventListener("health", (event) => {
      try {
        const data = JSON.parse(event.data);
        setPipelineStatus(data.status || "UNKNOWN");
      } catch {}
    });

    es.onerror = () => {
      setPipelineStatus("UNKNOWN");
      es.close();

      const interval = setInterval(async () => {
        try {
          const response = await fetch(`${BASE_URL}/ops/full-health`);
          if (response.ok) {
            const data = await response.json();
            setPipelineStatus(data.status || "UNKNOWN");
          }
        } catch {}
      }, 15000);

      return () => clearInterval(interval);
    };

    return () => es.close();
  }, []);

  const handleLogout = () => {
    logout();
    router.push("/login");
  };

  const dotColor =
    pipelineStatus === "HEALTHY" ? "bg-green-500" :
    pipelineStatus === "DEGRADED" ? "bg-yellow-500" :
    pipelineStatus === "CRITICAL" ? "bg-red-500 animate-pulse" :
    "bg-gray-400";

  return (
    <header className="bg-white border-b border-gray-200 pr-6 pl-12 md:pl-0 py-3 flex items-center justify-between">
      <h2 className="text-sm md:text-base font-semibold text-gray-800">{title}</h2>
      <div className="flex items-center gap-4">
        <a
          href="/chat"
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm font-semibold px-3 py-1.5 rounded-lg"
          style={{ background: "#6C63FF", color: "#fff", textDecoration: "none" }}
        >
          💬 AI Chat
        </a>
        <div className="flex items-center gap-1.5" title={`Pipeline: ${pipelineStatus}`}>
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${dotColor}`} />
          <span className="text-xs text-gray-500">{pipelineStatus}</span>
        </div>
        <span className="text-sm text-gray-500">{health}</span>
        <button
          type="button"
          onClick={handleLogout}
          className="text-sm text-gray-600 hover:text-gray-900 underline"
        >
          로그아웃
        </button>
      </div>
    </header>
  );
}
