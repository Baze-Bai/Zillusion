"use client";

const STAGE_LABELS: Record<string, string> = {
  parse_intent: "Parse Intent",
  // Agentic super-node — one stage covering the whole agentic discovery loop.
  agentic_discovery: "Agentic Discovery",
  judge: "Judge & Score",
  reflect: "Reflect",
  skill_writeback: "Skill Writeback",
  finalize: "Finalize",
};

interface PipelineProgressProps {
  stages: Record<string, { status: "pending" | "active" | "complete"; duration_ms?: number }>;
  currentStage: string;
  candidatesFound: number;
  costUsd: number;
}

export function PipelineProgress({
  stages,
  currentStage,
  candidatesFound,
  costUsd,
}: PipelineProgressProps) {
  const stageKeys = Object.keys(STAGE_LABELS);
  const completedCount = stageKeys.filter((s) => stages[s]?.status === "complete").length;

  return (
    <div className="bg-card border border-border rounded-xl p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-semibold text-sm">Pipeline Progress</h3>
        <div className="flex gap-4 text-xs text-muted-foreground">
          <span>{candidatesFound} candidates</span>
          <span>${costUsd.toFixed(4)}</span>
        </div>
      </div>

      {/* Progress bar */}
      <div className="w-full bg-muted rounded-full h-2 mb-4">
        <div
          className="bg-primary rounded-full h-2 transition-all duration-500"
          style={{ width: `${(completedCount / stageKeys.length) * 100}%` }}
        />
      </div>

      {/* Stage indicators — 6 columns after the agentic super-node refactor. */}
      <div className="grid grid-cols-6 gap-1">
        {stageKeys.map((key) => {
          const stage = stages[key];
          const status = stage?.status || "pending";
          const label = STAGE_LABELS[key];

          return (
            <div key={key} className="text-center">
              <div
                className={`w-6 h-6 mx-auto rounded-full flex items-center justify-center text-xs mb-1
                  ${status === "complete" ? "bg-green-500 text-white" : ""}
                  ${status === "active" ? "bg-blue-500 text-white animate-pulse" : ""}
                  ${status === "pending" ? "bg-muted text-muted-foreground" : ""}`}
              >
                {status === "complete" ? "\u2713" : status === "active" ? "\u2022" : ""}
              </div>
              <span
                className={`text-[10px] leading-tight block
                  ${status === "active" ? "text-primary font-medium" : "text-muted-foreground"}`}
              >
                {label}
              </span>
              {stage?.duration_ms !== undefined && (
                <span className="text-[9px] text-muted-foreground">
                  {(stage.duration_ms / 1000).toFixed(1)}s
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
