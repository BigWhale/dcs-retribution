import { deselectFlight, selectFlight } from "../../api/flightsSlice";
import { renderWithProviders } from "../../testutils";
import LiveUnitsLayer, { BLUE_COLOR } from "./LiveUnitsLayer";
import { act } from "@testing-library/react";
import ms from "milsymbol";
import { PropsWithChildren } from "react";

const mockLayerGroup = jest.fn();
const mockMarker = jest.fn();
const mockCircleMarker = jest.fn();
jest.mock("react-leaflet", () => ({
  LayerGroup: (props: PropsWithChildren<any>) => {
    mockLayerGroup(props);
    return <>{props.children}</>;
  },
  Marker: (props: PropsWithChildren<any>) => {
    mockMarker(props);
    return <>{props.children}</>;
  },
  CircleMarker: (props: PropsWithChildren<any>) => {
    mockCircleMarker(props);
    return <>{props.children}</>;
  },
  Tooltip: (props: PropsWithChildren<any>) => {
    return <>{props.children}</>;
  },
}));

// A spy called with `new` does not give the real symbol's prototype, so it
// builds the real symbol and returns it. Set before each test: the client's
// jest config resets mock implementations between tests.
const RealSymbol = ms.Symbol;
const symbolSpy = jest.spyOn(ms, "Symbol");

beforeEach(() => {
  mockLayerGroup.mockClear();
  mockMarker.mockClear();
  mockCircleMarker.mockClear();
  symbolSpy.mockImplementation(
    (...args: ConstructorParameters<typeof RealSymbol>) =>
      new RealSymbol(...args),
  );
});

const unit = (
  name: string,
  category: string,
  side: number,
  sidc: string,
  heading: number,
  speed: number,
  flight_id: string | null = null,
) => ({
  name: name,
  group: `${name} group`,
  category: category,
  type: "F-16C_50",
  side: side,
  position: { lat: 27.2, lng: 56.4 },
  altitude: 10,
  heading: heading,
  speed: speed,
  in_air: speed > 0,
  player: null,
  sidc: sidc,
  flight_id: flight_id,
});

// The icons are cached by look for the whole run, so each test draws codes
// of its own, or a symbol drawn by an earlier test would not be drawn again.
const renderUnits = (
  units: ReturnType<typeof unit>[],
  selected: string | null = null,
) =>
  renderWithProviders(
    <LiveUnitsLayer categories={["plane", "ground", "static"]} />,
    {
      preloadedState: {
        liveUnits: {
          units: Object.fromEntries(units.map((u) => [u.name, u])),
        },
        flights: { flights: {}, selected: selected },
      },
    },
  );

const symbolOptionsFor = (sidc: string) =>
  symbolSpy.mock.calls
    .filter(([code]) => code === sidc)
    .map(([, options]) => options as any);

test("layer is empty by default", async () => {
  renderWithProviders(<LiveUnitsLayer categories={["plane"]} />);
  expect(mockLayerGroup).toHaveBeenCalledTimes(1);
  expect(mockMarker).not.toHaveBeenCalled();
  expect(mockCircleMarker).not.toHaveBeenCalled();
});

test("draws a symbol per unit with its code", async () => {
  renderUnits([
    unit("Uzi 1-1", "plane", 2, "10030100001101000000", 90, 200),
    unit("Tank 1", "ground", 1, "10061000001201000000", 0, 0),
    unit("Hangar 1", "static", 2, "10000000000000000000", 0, 0),
  ]);
  expect(mockMarker).toHaveBeenCalledTimes(2);
  expect(mockCircleMarker).toHaveBeenCalledTimes(1);
  expect(mockCircleMarker.mock.calls[0][0].pathOptions.fillColor).toBe(
    BLUE_COLOR,
  );
  const codes = symbolSpy.mock.calls.map(([code]) => code);
  expect(codes.sort()).toEqual(
    ["10030100001101000000", "10061000001201000000"].sort(),
  );
});

test("tail only on a moving unit", async () => {
  renderUnits([
    unit("Colt 1-1", "plane", 2, "10030100001101040000", 90, 200),
    unit("Tank 2", "ground", 1, "10061000001211000000", 0, 0),
  ]);
  const [plane] = symbolOptionsFor("10030100001101040000");
  expect(plane.direction).toBe(90);
  expect(plane.speedLeader).toBeGreaterThan(0);
  const [tank] = symbolOptionsFor("10061000001211000000");
  expect(tank.direction).toBeUndefined();
  expect(tank.speedLeader ?? 0).toBe(0);
});

test("the selected flight's units stand out", async () => {
  const units = [
    unit("Enfield 1-1", "plane", 2, "10030100001101050000", 90, 200, "f-1"),
    unit("Enfield 1-2", "plane", 1, "10060100001101060000", 90, 200, "f-1"),
    unit("Tank 3", "ground", 1, "10061000001221000000", 0, 0),
    unit("Hangar 3", "static", 2, "10000000000000000000", 0, 0),
  ];
  const look = (sidc: string) =>
    symbolOptionsFor(sidc).map((options) => ({
      outlineColor: options.outlineColor,
      outlineWidth: options.outlineWidth,
      colorMode: options.colorMode,
      iconColor: options.iconColor,
    }));
  const plain = {
    outlineColor: undefined,
    outlineWidth: undefined,
    colorMode: undefined,
    iconColor: undefined,
  };
  const standingOut = {
    outlineColor: "yellow",
    outlineWidth: 20,
    colorMode: {
      Friend: "#003380",
      Hostile: "#990000",
      Neutral: "#006600",
      Unknown: "#7a6a00",
      Civilian: "#500050",
      Suspect: "#995c00",
    },
    iconColor: {
      Friend: "white",
      Hostile: "white",
      Neutral: "white",
      Unknown: "white",
      Civilian: "white",
      Suspect: "white",
    },
  };
  // The z-index offset each unit's marker was last drawn with.
  const zIndexOffsets = () =>
    Object.fromEntries(
      mockMarker.mock.calls.map(([props]) => [
        props.children.props.unit.name,
        props.zIndexOffset,
      ]),
    );

  // Drawn first with no flight selected, so the icons that stand out below
  // cannot come from the cache.
  const { store } = renderUnits(units, null);
  for (const u of units.slice(0, 3)) {
    expect(look(u.sidc)).toEqual([plain]);
  }
  expect(zIndexOffsets()).toEqual({
    "Enfield 1-1": 0,
    "Enfield 1-2": 0,
    "Tank 3": 0,
  });

  symbolSpy.mockClear();
  act(() => {
    store.dispatch(selectFlight("f-1"));
  });
  expect(look("10030100001101050000")).toEqual([standingOut]);
  expect(look("10060100001101060000")).toEqual([standingOut]);
  expect(look("10061000001221000000")).toEqual([]);
  expect(zIndexOffsets()).toEqual({
    "Enfield 1-1": 100000,
    "Enfield 1-2": 100000,
    "Tank 3": 0,
  });
  // Statics belong to no flight and keep their plain circle.
  const [hangar] = mockCircleMarker.mock.calls.at(-1)!;
  expect(hangar.pathOptions.fillColor).toBe(BLUE_COLOR);
  expect(hangar.zIndexOffset).toBeUndefined();

  // Deselected, the flight's markers drop back among the others.
  act(() => {
    store.dispatch(deselectFlight());
  });
  expect(zIndexOffsets()).toEqual({
    "Enfield 1-1": 0,
    "Enfield 1-2": 0,
    "Tank 3": 0,
  });
});
