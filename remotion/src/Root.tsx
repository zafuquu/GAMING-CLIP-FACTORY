import React from "react";
import { Composition } from "remotion";
import { GameplayClip, type GameplayProps } from "./Gameplay";

const defaults: GameplayProps = {
  sourceFile: "clip.mp4",
  sourceFps: 30,
  startSeconds: 0,
  durationSeconds: 2,
  profile: "fit_pad",
  captions: [],
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id="GamingClip"
    component={GameplayClip}
    width={1080}
    height={1920}
    fps={30}
    durationInFrames={60}
    defaultProps={defaults}
    calculateMetadata={({ props }) => ({
      durationInFrames: Math.max(1, Math.ceil(Number(props.durationSeconds) * 30)),
    })}
  />
);
