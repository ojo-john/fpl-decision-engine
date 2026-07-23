import type { CSSProperties, ReactNode } from "react";

export const inputStyle: CSSProperties = {
  background: "var(--bg)",
  border: "1px solid var(--border)",
  borderRadius: "6px",
  color: "var(--text)",
  padding: "0.45rem 0.6rem",
  fontSize: "0.9rem",
};

export const buttonStyle: CSSProperties = {
  background: "var(--accent)",
  border: "none",
  borderRadius: "6px",
  color: "white",
  padding: "0.45rem 0.85rem",
  fontSize: "0.9rem",
  cursor: "pointer",
};

export function Card({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <section
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
        padding: "1.25rem",
        ...style,
      }}
    >
      {children}
    </section>
  );
}

const CONFIDENCE_COLORS: Record<string, string> = {
  low: "var(--danger)",
  medium: "#e0a940",
  high: "var(--success)",
};

export function ConfidenceBadge({ confidence }: { confidence: string }) {
  const color = CONFIDENCE_COLORS[confidence.toLowerCase()] ?? "var(--text-muted)";
  return (
    <span
      style={{
        display: "inline-block",
        padding: "0.15rem 0.55rem",
        borderRadius: "999px",
        fontSize: "0.75rem",
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.03em",
        color,
        border: `1px solid ${color}`,
      }}
    >
      {confidence}
    </span>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <Card>
      <p style={{ color: "var(--text-muted)", margin: 0 }}>{children}</p>
    </Card>
  );
}

export function ErrorState({ children }: { children: ReactNode }) {
  return (
    <Card style={{ borderColor: "var(--danger)" }}>
      <p style={{ color: "var(--danger)", margin: 0 }}>{children}</p>
    </Card>
  );
}
