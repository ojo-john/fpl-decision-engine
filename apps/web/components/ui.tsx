import type { CSSProperties, ReactNode } from "react";

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
