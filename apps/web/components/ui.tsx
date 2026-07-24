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

const PRICE_MOMENTUM_LABELS: Record<string, string> = {
  likely_rise: "Likely to rise",
  likely_fall: "Likely to fall",
  stable: "Stable",
  unknown: "Unknown",
};

const PRICE_MOMENTUM_COLORS: Record<string, string> = {
  likely_rise: "var(--success)",
  likely_fall: "var(--danger)",
  stable: "var(--text-muted)",
  unknown: "var(--text-muted)",
};

/** See app.players.price_signal -- an explainable heuristic, not FPL's real (undisclosed)
 * price-change algorithm. */
export function PriceMomentumBadge({ momentum }: { momentum: string }) {
  const color = PRICE_MOMENTUM_COLORS[momentum] ?? "var(--text-muted)";
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
      {PRICE_MOMENTUM_LABELS[momentum] ?? momentum}
    </span>
  );
}

/** Wraps a <table> so it scrolls horizontally within its own box on a narrow viewport instead
 * of blowing out the page's width -- see the ".table-scroll" rule in app/globals.css. */
export function TableScroll({ children }: { children: ReactNode }) {
  return <div className="table-scroll">{children}</div>;
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
