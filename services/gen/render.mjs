// One Remotion part, run by server.mjs as a child process (killed on cancel). Reads the job as JSON on stdin and
// writes JSON lines on stdout: {type:"info", frames_total, fps, width, height, seconds}, {type:"progress",
// frames_done, progress}, {type:"done", file, frames, seconds}, {type:"error", code, message[, frames_total]}.
import { renderMedia, selectComposition } from "@remotion/renderer";

const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
let input = "";
for await (const chunk of process.stdin) input += chunk;
const job = JSON.parse(input);
const chromiumOptions = { gl: job.gl || null };

try {
  const comp = await selectComposition({ serveUrl: job.serveUrl, id: job.composition, inputProps: job.props || {},
                                         chromiumOptions, logLevel: "error" });
  const info = { frames_total: comp.durationInFrames, fps: comp.fps, width: comp.width, height: comp.height,
                 seconds: comp.durationInFrames / comp.fps };
  out({ type: "info", ...info });
  if (job.part === "info") process.exit(0);
  const [a, b] = job.frames || [0, comp.durationInFrames - 1];
  if (b >= comp.durationInFrames) {
    out({ type: "error", code: "frames_out_of_range", frames_total: comp.durationInFrames,
          message: `frames ${a}-${b}: the composition has ${comp.durationInFrames} frames (0-${comp.durationInFrames - 1})` });
    process.exit(3);
  }
  const frames = b - a + 1;
  let last = 0;
  const common = { composition: comp, serveUrl: job.serveUrl, inputProps: job.props || {}, frameRange: [a, b],
                   outputLocation: job.output, chromiumOptions, logLevel: "error", overwrite: true,
                   concurrency: job.concurrency || null };
  if (job.part === "audio") {
    // audio only: WAV (Remotion's sample rate is 48 kHz); the coordinator encodes aac/opus when muxing
    await renderMedia({ ...common, codec: "wav" });
  } else {
    await renderMedia({
      ...common, codec: job.codec, crf: job.crf, pixelFormat: job.pix_fmt, muted: true,
      x264Preset: job.codec === "h264" ? "medium" : undefined,  // the same encoder settings on every node
      onProgress: ({ renderedFrames, progress }) => {
        const now = Date.now();
        if (now - last > 1000 || progress >= 1) {
          last = now;
          out({ type: "progress", frames_done: renderedFrames, progress: Math.min(1, renderedFrames / frames) });
        }
      },
    });
  }
  out({ type: "done", file: job.output, frames, seconds: frames / comp.fps });
} catch (e) {
  const msg = String(e && e.message || e);
  const code = /Could not find composition|No composition with the ID/i.test(msg) ? "no_composition" : "render_failed";
  out({ type: "error", code, message: msg.slice(0, 500) });
  process.exit(2);
}
