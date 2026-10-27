import React from "react";
import { AbsoluteFill, OffthreadVideo, staticFile, useCurrentFrame } from "remotion";

export type Caption = {
  startFrame: number;
  endFrame: number;
  text: string;
};

export type GameplayProps = {
  sourceFile: string;
  sourceFps: number;
  startSeconds: number;
  durationSeconds: number;
  profile: "fit_pad" | "blur_fill";
  captions: Caption[];
};

const FPS = 30;

export const GameplayClip: React.FC<GameplayProps> = ({
  sourceFile,
  sourceFps,
  startSeconds,
  profile,
  captions,
}) => {
  const frame = useCurrentFrame();
  const source = staticFile(sourceFile);
  const trimBefore = Math.max(0, Math.round(startSeconds * sourceFps));
  const activeCaption = captions.find(
    (caption) => frame >= caption.startFrame && frame < caption.endFrame,
  );

  const foregroundStyle: React.CSSProperties = {
    width: "100%",
    height: "100%",
    objectFit: "contain",
  };

  return (
    <AbsoluteFill style={{ backgroundColor: "#000", overflow: "hidden" }}>
      {profile === "blur_fill" ? (
        <>
          <OffthreadVideo
            src={source}
            trimBefore={trimBefore}
            muted
            style={{
              position: "absolute",
              width: "100%",
              height: "100%",
              objectFit: "cover",
              filter: "blur(34px) brightness(0.55) saturate(0.82)",
              transform: "scale(1.12)",
            }}
          />
          <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
            <OffthreadVideo
              src={source}
              trimBefore={trimBefore}
              style={foregroundStyle}
            />
          </AbsoluteFill>
        </>
      ) : (
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
          <OffthreadVideo
            src={source}
            trimBefore={trimBefore}
            style={foregroundStyle}
          />
        </AbsoluteFill>
      )}

      {activeCaption && (
        <div
          style={{
            position: "absolute",
            left: "7%",
            right: "7%",
            bottom: 230,
            display: "flex",
            justifyContent: "center",
            textAlign: "center",
            color: "#fff",
            fontFamily: "Arial, Helvetica, sans-serif",
            fontSize: 48,
            fontWeight: 900,
            lineHeight: 1.12,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            WebkitTextStroke: "1.8px #080808",
            paintOrder: "stroke fill",
            textShadow: "0 4px 10px rgba(0,0,0,0.9)",
          }}
        >
          {activeCaption.text}
        </div>
      )}
    </AbsoluteFill>
  );
};
