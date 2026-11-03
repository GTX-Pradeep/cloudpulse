const { build } = require("./builder");
const parts = [require("./part1"), require("./part2")];
try { parts.push(require("./part3")); } catch (e) { if (e.code !== "MODULE_NOT_FOUND") throw e; }
const blocks = parts.flatMap((p) => p());
build("CloudPulse", "Complete technical guide: every component, algorithm, decision, result and mistake",
  ["Experiment-validated PostgreSQL query regression remediation",
   "Repository: github.com/GTX-Pradeep/cloudpulse",
   `Generated from the repository on ${new Date().toISOString().slice(0, 10)}`],
  blocks, process.argv[2] || "CloudPulse_Complete_Guide.docx").then(() => console.log("ok", blocks.length, "blocks"));
