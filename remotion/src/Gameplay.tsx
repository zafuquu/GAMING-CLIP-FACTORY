import React from "react";
import { AbsoluteFill, OffthreadVideo, staticFile, useCurrentFrame, useVideoConfig } from "remotion";

export type Caption = {
  startFrame: number;
  endFrame: number;
  text: string;
};

export type GameplayProps = {
  sourceFile: string;
  startSeconds: number;
  durationSeconds: number;
  profile: "fit_pad" | "blur_fill";
  captions: Caption[];
};

export const GameplayClip: React.FC<GameplayProps> = ({
  sourceFile,
  startSeconds,
  profile,
  captions,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const source = staticFile(sourceFile);
  // trimBefore is measured in composition frames, not source-media frames.
  const trimBefore = Math.max(0, Math.round(startSeconds * fps));
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
            left: "6%",
            right: "15%",
            bottom: 280,
            display: "flex",
            justifyContent: "center",
            textAlign: "center",
            color: "#fff",
            fontFamily: "Arial, Helvetica, sans-serif",
            fontSize: activeCaption.text.length > 100 ? 38 : activeCaption.text.length > 60 ? 42 : 46,
            fontWeight: 800,
            lineHeight: 1.12,
            whiteSpace: "pre-wrap",
            overflowWrap: "break-word",
            padding: "14px 22px",
            borderRadius: 16,
            backgroundColor: "rgba(0, 0, 0, 0.62)",
            boxShadow: "0 8px 26px rgba(0, 0, 0, 0.4)",
            maxHeight: 250,
            overflow: "hidden",
            WebkitTextStroke: "1px rgba(0, 0, 0, 0.9)",
            paintOrder: "stroke fill",
            textShadow: "0 3px 8px rgba(0,0,0,0.85)",
          }}
        >
          {activeCaption.text}
        </div>
      )}
    </AbsoluteFill>
  );
};
