"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";

const navItems = [
  { href: "/", label: "Dashboard", icon: "🏠" },
  { href: "/chat", label: "AI Chat", icon: "💬", highlight: true },
  { href: "/braming", label: "브레인스토밍", icon: "🧠" },
  { href: "/project-status", label: "Project Status", icon: "📊" },
  { href: "/conversations", label: "Conversations", icon: "🗨️" },
  { href: "/channels", label: "대화창 관리", icon: "📌" },
  { href: "/managers", label: "Managers", icon: "👥" },
  { href: "/agenda", label: "아젠다", icon: "📌" },
  { href: "/decisions", label: "CEO Decisions", icon: "🎯" },
  { href: "/tasks", label: "Tasks", icon: "📋" },
  { href: "/docs", label: "문서 통합", icon: "📄" },
  { href: "/projects", label: "Pipeline", icon: "🔧" },
  { href: "/ops", label: "운영 현황", icon: "📊" },
  { href: "/ops/recovery", label: "Recovery", icon: "🔄" },
  { href: "/ops/servers", label: "Servers", icon: "🖥️" },
  { href: "/ops/memory", label: "메모리", icon: "🧠" },
  { href: "/ops/pc-agents", label: "PC Agent", icon: "💻" },
  { href: "/lessons", label: "교훈", icon: "💡" },
  { href: "/flow", label: "FLOW", icon: "🔄" },
  { href: "/reports", label: "Reports", icon: "📊" },
  { href: "/kakaobot", label: "KakaoBot", icon: "💬" },
  { href: "/settings", label: "Settings", icon: "⚙️" },
  { href: "/admin/prompts", label: "Prompts", icon: "📝" },
  { href: "/admin/tasks", label: "Task Board", icon: "🗂️" },
  { href: "/admin/agents", label: "Agent Registry", icon: "🧩" },
  { href: "/server-status", label: "Server Status", icon: "🖥️" },
];

interface SidebarProps {
  isOpen: boolean;
  onOpen: () => void;
  onClose: () => void;
}

export default function Sidebar({ isOpen, onOpen, onClose }: SidebarProps) {
  const pathname = usePathname();

  useEffect(() => {
    onClose();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  return (
    <>
      <button
        className="fixed top-3 left-3 z-50 md:hidden text-white rounded p-2 leading-none"
        style={{ background: "var(--bg-card)" }}
        onClick={onOpen}
        aria-label="메뉴 열기"
      >
        ☰
      </button>

      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 md:hidden"
          onClick={onClose}
        />
      )}

      <aside
        className={`
          fixed top-0 left-0 h-full z-50 w-56 flex flex-col
          transition-transform duration-300
          ${isOpen ? "translate-x-0" : "-translate-x-full"}
          md:relative md:translate-x-0 md:h-screen md:z-auto
        `}
        style={{ background: "var(--bg-card)", color: "var(--text-primary)", borderRight: "1px solid var(--border)" }}
      >
        <div className="p-4 flex items-center justify-between" style={{ borderBottom: "1px solid var(--border)" }}>
          <div>
            <h1 className="text-lg font-bold" style={{ color: "var(--accent)" }}>AADS</h1>
            <p className="text-xs" style={{ color: "var(--text-secondary)" }}>Autonomous AI Dev System</p>
          </div>
          <button
            className="md:hidden text-lg leading-none"
            style={{ color: "var(--text-secondary)" }}
            onClick={onClose}
            aria-label="메뉴 닫기"
          >
            ✕
          </button>
        </div>
        <nav className="flex-1 p-3 space-y-1">
          {navItems.map((item) => {
            const isActive = pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href));
            return (
              <Link
                key={item.href}
                href={item.href}
                className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors"
                style={isActive
                  ? { background: "var(--accent)", color: "#fff" }
                  : "highlight" in item && item.highlight
                    ? { color: "#a78bfa", fontWeight: 600 }
                    : { color: "var(--text-secondary)" }
                }
                onMouseEnter={e => { if (!isActive) (e.currentTarget as HTMLElement).style.background = "var(--bg-hover)"; }}
                onMouseLeave={e => { if (!isActive) (e.currentTarget as HTMLElement).style.background = ""; }}
              >
                <span>{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="p-3" style={{ borderTop: "1px solid var(--border)" }}>
          <a
            href="/chat"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors w-full"
            style={{ background: "var(--accent)", color: "#fff" }}
            onMouseEnter={e => ((e.currentTarget as HTMLElement).style.background = "var(--accent-hover)")}
            onMouseLeave={e => ((e.currentTarget as HTMLElement).style.background = "var(--accent)")}
          >
            <span>💬</span>
            <span>AI Chat</span>
          </a>
          <div className="mt-2 text-xs text-center" style={{ color: "var(--text-secondary)" }}>
            v0.5.1 · Server Status
          </div>
        </div>
      </aside>
    </>
  );
}
