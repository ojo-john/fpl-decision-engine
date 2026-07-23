"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/rivals", label: "Rivals" },
  { href: "/learning", label: "Learning" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();

  async function handleLogout() {
    await fetch("/api/session", { method: "DELETE" });
    router.push("/login");
    router.refresh();
  }

  return (
    <nav
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0.85rem 1.5rem",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <div style={{ display: "flex", gap: "1.25rem", alignItems: "center" }}>
        <strong>FPL Decision Engine</strong>
        {LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            style={{
              color: pathname === link.href ? "var(--text)" : "var(--text-muted)",
              textDecoration: "none",
              fontSize: "0.9rem",
            }}
          >
            {link.label}
          </Link>
        ))}
      </div>
      <button
        onClick={handleLogout}
        style={{
          background: "transparent",
          border: "1px solid var(--border)",
          borderRadius: "6px",
          color: "var(--text-muted)",
          padding: "0.4rem 0.75rem",
          cursor: "pointer",
          fontSize: "0.85rem",
        }}
      >
        Log out
      </button>
    </nav>
  );
}
