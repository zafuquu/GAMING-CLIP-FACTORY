import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const node = process.execPath;
const ffmpeg = process.env.GCF_FFMPEG_PATH || (process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg");
const ffprobe = process.env.GCF_FFPROBE_PATH || (process.platform === "win32" ? "ffprobe.exe" : "ffprobe");

function run(command, args, timeout = 120000) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    windowsHide: true,
    timeout,
    shell: false,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited ${result.status}: ${(result.stderr || result.stdout || "").slice(-3000)}`);
  }
  return result;
}

const workDir = await mkdtemp(path.join(os.tmpdir(), "gcf-remotion-smoke-"));
try {
  const mediaDir = path.join(workDir, "media");
  const outputDir = path.join(workDir, "outputs");
  const sourcePath = path.join(mediaDir, "synthetic.mp4");
  const outputPath = path.join(outputDir, "rendered-smoke.mp4");
  const propsPath = path.join(workDir, "props.json");
  const { mkdir } = await import("node:fs/promises");
  await mkdir(mediaDir, { recursive: true });
  await mkdir(outputDir, { recursive: true });

  run(ffmpeg, [
    "-hide_banner", "-loglevel", "error", "-nostdin",
    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=4",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "96k", "-y", sourcePath,
  ]);

  await writeFile(propsPath, JSON.stringify({
    sourcePath,
    outputPath,
    startSeconds: 0.5,
    durationSeconds: 2,
    sourceFps: 24,
    profile: "fit_pad",
    captions: [{ startFrame: 5, endFrame: 25, text: "Synthetic render test" }],
  }), "utf8");

  run(node, [path.join(ROOT, "render.mjs"), "--props-file", propsPath], 240000);

  const probe = run(ffprobe, [
    "-v", "error", "-show_streams", "-show_format", "-of", "json", outputPath,
  ]);
  const result = JSON.parse(probe.stdout);
  const video = result.streams.find((stream) => stream.codec_type === "video");
  const audio = result.streams.find((stream) => stream.codec_type === "audio");
  const duration = Number(result.format.duration);
  const outputStat = await stat(outputPath);

  if (!video || video.codec_name !== "h264" || video.width !== 1080 || video.height !== 1920) {
    throw new Error("Smoke render did not produce H.264 video at 1080x1920.");
  }
  if (!audio || audio.codec_name !== "aac") {
    throw new Error("Smoke render did not preserve source audio as AAC.");
  }
  if (!Number.isFinite(duration) || duration < 1.5 || duration > 2.5) {
    throw new Error(`Smoke render duration ${duration} does not match the requested 2 seconds.`);
  }
  if (outputStat.size <= 1000) {
    throw new Error("Smoke render output is unexpectedly small.");
  }

  process.stdout.write(JSON.stringify({
    ok: true,
    renderer: "remotion",
    outputPath,
    bytes: outputStat.size,
    video: { codec: video.codec_name, width: video.width, height: video.height },
    audio: { codec: audio.codec_name, sampleRate: audio.sample_rate },
    durationSeconds: duration,
  }, null, 2) + "\n");
} finally {
  await rm(workDir, { recursive: true, force: true });
}
