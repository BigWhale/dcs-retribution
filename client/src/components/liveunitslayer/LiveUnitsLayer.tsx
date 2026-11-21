import { LiveUnit } from "../../api/liberationApi";
import { selectLiveUnits } from "../../api/liveUnitsSlice";
import { useAppSelector } from "../../app/hooks";
import { PathOptions } from "leaflet";
import { memo } from "react";
import { CircleMarker, LayerGroup, Tooltip } from "react-leaflet";

// Fill colours by DCS coalition: 2 blue, 1 red, 0 neutral. Bright enough to
// read on the imagery base layers, with a dark outline for the topographic one.
export const BLUE_COLOR = "#3399ff";
export const RED_COLOR = "#ff3b3b";
export const NEUTRAL_COLOR = "#ffd400";

// A fixed pixel radius: a unit stays visible zoomed out, and a ramp full of
// parked aircraft stays a readable cluster of small dots.
const RADIUS = 4;

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

function LiveUnitMarker(props: LiveUnitMarkerProps) {
  const unit = props.unit;
  return (
    <CircleMarker
      center={unit.position}
      radius={RADIUS}
      pathOptions={PATH_OPTIONS[unit.side] ?? PATH_OPTIONS[0]}
    >
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
    </CircleMarker>
  );
}

// Each snapshot brings new objects for every unit; a unit whose drawn fields
// did not change (a parked aircraft) is not rendered again.
const sameDrawnUnit = (a: LiveUnitMarkerProps, b: LiveUnitMarkerProps) =>
  a.unit.position.lat === b.unit.position.lat &&
  a.unit.position.lng === b.unit.position.lng &&
  a.unit.side === b.unit.side &&
  a.unit.type === b.unit.type &&
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
