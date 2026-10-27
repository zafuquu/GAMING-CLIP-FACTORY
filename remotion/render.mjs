import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { constants } from "node:fs";
import { access, link, mkdir, mkdtemp, readFile, rm, stat, unlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const FPS = 30;

function fail(message) {
  throw new Error(message);
}

async function exists(file) {
  try {
    await access(file, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

function parseArgs(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith("--") || !argv[i + 1]) {
      fail("Usage: node render.mjs --props-file <path-to-json>");
    }
    result[key.slice(2)] = argv[i + 1];
    i += 1;
  }
  if (!result["props-file"]) {
    fail("Missing required --props-file argument.");
  }
  return result;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const propsPath = path.resolve(args["props-file"]);
  const raw = JSON.parse(await readFile(propsPath, "utf8"));
  const sourcePath = path.resolve(String(raw.sourcePath || ""));
  const outputPath = path.resolve(String(raw.outputPath || ""));
  const sourceInfo = await stat(sourcePath).catch(() => null);
  if (!sourceInfo?.isFile() || sourceInfo.size <= 0) {
    fail("Remotion source media is missing or empty.");
  }
  if (!outputPath.toLowerCase().endsWith(".mp4") || sourcePath === outputPath) {
    fail("Remotion output must be an MP4 path different from the input.");
  }
  const durationSeconds = Number(raw.durationSeconds);
  const startSeconds = Number(raw.startSeconds);
  const sourceFps = Number(raw.sourceFps);
  const profile = raw.profile === "blur_fill" ? "blur_fill" : raw.profile === "fit_pad" ? "fit_pad" : null;
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0 || durationSeconds > 600) {
    fail("Remotion duration must be greater than 0 and no more than 600 seconds.");
  }
  if (!Number.isFinite(startSeconds) || startSeconds < 0) {
    fail("Remotion startSeconds must be a finite non-negative number.");
  }
  if (!Number.isFinite(sourceFps) || sourceFps < 1 || sourceFps > 240) {
    fail("Remotion sourceFps must be between 1 and 240.");
  }
  if (!profile) {
    fail("Unsupported Remotion profile. Expected fit_pad or blur_fill.");
  }
  if (await exists(outputPath)) {
    fail("Remotion refuses to overwrite an existing output file.");
  }

  const sourceExtension = path.extname(sourcePath).toLowerCase();
  if (![".mp4", ".webm"].includes(sourceExtension)) {
    fail("Remotion browser rendering currently requires an .mp4 or .webm source file. Use GCF_RENDER_ENGINE=ffmpeg for other containers.");
  }

  const tempPublicDir = await mkdtemp(path.join(path.dirname(sourcePath), ".gcf-remotion-public-"));
  const sourceAlias = path.join(tempPublicDir, "clip" + sourceExtension);
  let succeeded = false;

  try {
    // A hard link avoids copying multi-gigabyte gameplay files. The temporary public
    // directory contains only this asset, so Remotion's local asset server cannot expose
    // the rest of the user's media folder.
    await link(sourcePath, sourceAlias).catch((error) => {
      throw new Error(
        "Could not safely create a temporary hard link to the source video. " +
        "Use an NTFS/local filesystem that supports hard links, or switch GCF_RENDER_ENGINE=ffmpeg. " +
        `Underlying error: ${error.code || error.name}`,
      );
    });

    const captions = Array.isArray(raw.captions)
      ? raw.captions
          .filter((caption) =>
            caption &&
            Number.isFinite(Number(caption.startFrame)) &&
            Number.isFinite(Number(caption.endFrame)) &&
            Number(caption.startFrame) >= 0 &&
            Number(caption.endFrame) > Number(caption.startFrame) &&
            typeof caption.text === "string",
          )
          .map((caption) => ({
            startFrame: Math.max(0, Math.floor(Number(caption.startFrame))),
            endFrame: Math.max(1, Math.ceil(Number(caption.endFrame))),
            text: caption.text.slice(0, 500),
          }))
      : [];

    const inputProps = {
      sourceFile: "clip.asset",
      sourceFps,
      startSeconds,
      durationSeconds,
      profile,
      captions,
    };

    const serveUrl = await bundle({
      entryPoint: path.join(ROOT, "src", "index.ts"),
      publicDir: tempPublicDir,
      onProgress: () => {},
    });

    const composition = await selectComposition({
      serveUrl,
      id: "GamingClip",
      inputProps,
    });

    await mkdir(path.dirname(outputPath), { recursive: true });
    await renderMedia({
      composition,
      serveUrl,
      outputLocation: outputPath,
      inputProps,
      codec: "h264",
      audioCodec: "aac",
      audioBitrate: 128000,
      pixelFormat: "yuv420p",
      crf: 21,
      x264Preset: "veryfast",
      concurrency: Math.max(1, Math.min(2, os.cpus().length)),
      logLevel: "error",
    });

    const outputInfo = await stat(outputPath).catch(() => null);
    if (!outputInfo?.isFile() || outputInfo.size <= 0) {
      fail("Remotion finished without producing a non-empty MP4.");
    }
    succeeded = true;
    process.stdout.write(JSON.stringify({
      ok: true,
      renderer: "remotion",
      outputPath,
      fileSize: outputInfo.size,
      durationSeconds,
      width: 1080,
      height: 1920,
    }) + "\n");
  } finally {
    if (!succeeded) {
      await unlink(outputPath).catch(() => {});
    }
    await rm(tempPublicDir, { recursive: true, force: true }).catch(() => {});
  }
}

main().then(
  () => process.exit(0),
  (error) => {
    process.stderr.write(`Remotion render failed: ${error?.stack || error}\n`);
    process.exit(1);
  },
);
