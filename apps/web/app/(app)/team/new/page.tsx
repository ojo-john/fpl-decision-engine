import { Card } from "@/components/ui";
import { ManualSquadForm } from "./manual-squad-form";

export default function ManualSquadPage() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <h1 style={{ margin: 0, fontSize: "1.1rem" }}>Enter a squad manually</h1>
      <Card>
        <ManualSquadForm />
      </Card>
    </div>
  );
}
