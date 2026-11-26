import { AbsoluteFill, Audio, Composition, interpolate, staticFile, useCurrentFrame } from "remotion";

// 120 frames at 30 fps, 320x180: a moving bar, the frame number and a 4 s tone
const Fixture = ({ title }) => {
  const frame = useCurrentFrame();
  const x = interpolate(frame, [0, 119], [0, 280]);
  return (
    <AbsoluteFill style={{ backgroundColor: "#123", color: "white", fontSize: 28, fontFamily: "DejaVu Sans" }}>
      <div style={{ position: "absolute", left: x, top: 120, width: 40, height: 40, background: "#fc3" }} />
      <div style={{ padding: 12 }}>{title} {frame}</div>
      <Audio src={staticFile("tone.wav")} />
    </AbsoluteFill>
  );
};

export const Root = () => (
  <Composition id="Fixture" component={Fixture} durationInFrames={120} fps={30} width={320} height={180}
               defaultProps={{ title: "VoxArbor" }} />
);
