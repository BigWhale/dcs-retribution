import { LiveUnit } from "../../api/liberationApi";
import { selectLiveUnits } from "../../api/liveUnitsSlice";
import { useAppSelector } from "../../app/hooks";
import { Icon, PathOptions, Point } from "leaflet";
import ms from "milsymbol";
import { memo } from "react";
import { CircleMarker, LayerGroup, Marker, Tooltip } from "react-leaflet";

// Fill colours by DCS coalition: 2 blue, 1 red, 0 neutral. Bright enough to
// read on the imagery base layers, with a dark outline for the topographic one.
export const BLUE_COLOR = "#3399ff";
export const RED_COLOR = "#ff3b3b";
export const NEUTRAL_COLOR = "#ffd400";

// A fixed pixel radius for statics: they stay visible zoomed out, and a ramp
// full of them stays a readable cluster of small dots.
const RADIUS = 4;

// The units drawn as NATO symbols; the rest (statics) stay circles.
const SYMBOL_CATEGORIES = ["plane", "helicopter", "ground", "ship"];

// Symbol size in pixels, as the map draws flights.
const SYMBOL_SIZE = 20;

// A unit at or below this speed (m/s) is standing still and has no tail.
export const MOVING_SPEED = 1;

// The tail (milsymbol's speed leader) in pixels by speed in m/s: a few steps
// from a vehicle on a road to a fast jet, so icons cache well.
const TAIL_STEPS: { above: number; length: number }[] = [
  { above: 175, length: 32 },
  { above: 75, length: 24 },
  { above: 15, length: 16 },
  { above: MOVING_SPEED, length: 8 },
];

const tailLength = (speed: number) =>
  TAIL_STEPS.find((step) => speed > step.above)?.length ?? 0;

// Headings are drawn to the nearest 5 degrees, so icons cache well.
const HEADING_STEP = 5;

const roundedHeading = (heading: number) =>
  (((Math.round(heading / HEADING_STEP) * HEADING_STEP) % 360) + 360) % 360;

// Icons by look (code, rounded heading, tail length). A minute's snapshot
// moves most units only a little, so it reuses most icons; the same object
// also keeps react-leaflet from calling setIcon on an unchanged marker.
const iconCache = new Map<string, Icon>();

export function liveUnitIcon(unit: LiveUnit): Icon {
  const tail = tailLength(unit.speed);
  const heading = roundedHeading(unit.heading);
  const key = tail > 0 ? `${unit.sidc}/${heading}/${tail}` : unit.sidc;
  const cached = iconCache.get(key);
  if (cached) {
    return cached;
  }
  const symbol = new ms.Symbol(
    unit.sidc,
    tail > 0
      ? { size: SYMBOL_SIZE, direction: heading, speedLeader: tail }
      : { size: SYMBOL_SIZE },
  );
  const icon = new Icon({
    iconUrl: symbol.toDataURL(),
    iconAnchor: new Point(symbol.getAnchor().x, symbol.getAnchor().y),
  });
  iconCache.set(key, icon);
  return icon;
}

const pathOptionsFor = (fillColor: string): PathOptions => ({
  color: "#101010",
  weight: 1,
  opacity: 0.8,
  fillColor: fillColor,
  fillOpacity: 0.9,
});

// One shared object per side, so a render does not hand every marker a new
// pathOptions (react-leaflet calls setStyle when the reference changes).
const PATH_OPTIONS: { [side: number]: PathOptions } = {
  2: pathOptionsFor(BLUE_COLOR),
  1: pathOptionsFor(RED_COLOR),
  0: pathOptionsFor(NEUTRAL_COLOR),
};

const FEET_PER_METRE = 3.28084;
const KNOTS_PER_MS = 1.943844;

interface LiveUnitMarkerProps {
  unit: LiveUnit;
}

function LiveUnitTooltip(props: LiveUnitMarkerProps) {
  const unit = props.unit;
  return (
    <Tooltip>
      <b>{unit.name}</b>
      <br />
      {unit.type}
      <br />
      {Math.round(unit.altitude * FEET_PER_METRE)} ft,{" "}
      {Math.round(unit.speed * KNOTS_PER_MS)} kt
      {unit.player && (
        <>
          <br />
          {unit.player}
        </>
      )}
    </Tooltip>
  );
}

function LiveUnitMarker(props: LiveUnitMarkerProps) {
  const unit = props.unit;
  if (SYMBOL_CATEGORIES.includes(unit.category)) {
    return (
      <Marker position={unit.position} icon={liveUnitIcon(unit)}>
        <LiveUnitTooltip unit={unit} />
      </Marker>
    );
  }
  return (
    <CircleMarker
      center={unit.position}
      radius={RADIUS}
      pathOptions={PATH_OPTIONS[unit.side] ?? PATH_OPTIONS[0]}
    >
      <LiveUnitTooltip unit={unit} />
    </CircleMarker>
  );
}

// Each snapshot brings new objects for every unit; a unit whose drawn fields
// did not change (a parked aircraft) is not rendered again.
const sameDrawnUnit = (a: LiveUnitMarkerProps, b: LiveUnitMarkerProps) =>
  a.unit.position.lat === b.unit.position.lat &&
  a.unit.position.lng === b.unit.position.lng &&
  a.unit.side === b.unit.side &&
  a.unit.category === b.unit.category &&
  a.unit.sidc === b.unit.sidc &&
  a.unit.type === b.unit.type &&
  a.unit.heading === b.unit.heading &&
  a.unit.altitude === b.unit.altitude &&
  a.unit.speed === b.unit.speed &&
  a.unit.player === b.unit.player;

const MemoLiveUnitMarker = memo(LiveUnitMarker, sameDrawnUnit);

interface LiveUnitsLayerProps {
  categories: string[];
}

export default function LiveUnitsLayer(props: LiveUnitsLayerProps) {
  const units = useAppSelector(selectLiveUnits).units;
  return (
    <LayerGroup>
      {Object.values(units)
        .filter((unit) => props.categories.includes(unit.category))
        .map((unit) => (
          <MemoLiveUnitMarker key={unit.name} unit={unit} />
        ))}
    </LayerGroup>
  );
}
