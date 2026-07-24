import { Nav } from "@/components/nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Nav />
      <main className="app-main" style={{ maxWidth: "960px", margin: "0 auto" }}>
        {children}
      </main>
    </>
  );
}
