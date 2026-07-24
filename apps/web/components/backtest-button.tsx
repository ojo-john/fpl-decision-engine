"use client";

import { useRouter } from "next/navigation";
import { ActionButton } from "@/components/action-button";

export function BacktestButton({ modelVersionId }: { modelVersionId: string }) {
  const router = useRouter();

  return (
    <ActionButton
      path="/jobs/backtest"
      body={{ model_version_id: modelVersionId }}
      label="Run backtest on current champion"
      pendingLabel="Backtesting..."
      onSuccess={(data) => {
        const body = data as { backtest_run_id: string };
        router.push(`/backtest/${body.backtest_run_id}`);
      }}
    />
  );
}
