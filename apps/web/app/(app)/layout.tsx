import { Nav } from "@/components/nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Nav />
      <main style={{ maxWidth: "960px", margin: "0 auto", padding: "1.5rem" }}>{children}</main>
    </>
  );
}
