import { renderWithProviders } from "../../testutils";
import LiveUnitsLayer, {
  BLUE_COLOR,
  NEUTRAL_COLOR,
  RED_COLOR,
} from "./LiveUnitsLayer";
import { PropsWithChildren } from "react";

const mockLayerGroup = jest.fn();
const mockCircleMarker = jest.fn();
jest.mock("react-leaflet", () => ({
  LayerGroup: (props: PropsWithChildren<any>) => {
    mockLayerGroup(props);
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

beforeEach(() => {
  mockLayerGroup.mockClear();
  mockCircleMarker.mockClear();
});

const unit = (name: string, category: string, side: number) => ({
  name: name,
  group: `${name} group`,
  category: category,
  type: "F-16C_50",
  side: side,
  position: { lat: 27.2, lng: 56.4 },
  altitude: 10,
  heading: 0,
  speed: 0,
  in_air: false,
  player: null,
});

test("layer is empty by default", async () => {
  renderWithProviders(<LiveUnitsLayer categories={["plane"]} />);
  expect(mockLayerGroup).toHaveBeenCalledTimes(1);
  expect(mockCircleMarker).not.toHaveBeenCalled();
});

test("draws one circle per unit of its categories in its side's colour", async () => {
  renderWithProviders(
    <LiveUnitsLayer categories={["plane", "ground", "ship"]} />,
    {
      preloadedState: {
        liveUnits: {
          units: {
            "Uzi 1-1": unit("Uzi 1-1", "plane", 2),
            "Tank 1": unit("Tank 1", "ground", 1),
            "Boat 1": unit("Boat 1", "ship", 0),
            "Hangar 1": unit("Hangar 1", "static", 2),
          },
        },
      },
    },
  );
  expect(mockCircleMarker).toHaveBeenCalledTimes(3);
  const fills = mockCircleMarker.mock.calls.map(
    ([props]) => props.pathOptions.fillColor,
  );
  expect(fills.sort()).toEqual([BLUE_COLOR, RED_COLOR, NEUTRAL_COLOR].sort());
});
