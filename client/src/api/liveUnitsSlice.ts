import { RootState } from "../app/store";
import { gameLoaded, gameUnloaded } from "./actions";
import { LiveUnit } from "./liberationApi";
import { PayloadAction, createSlice } from "@reduxjs/toolkit";

// The units of the running DCS mission, as the companion's newest snapshot
// has them, keyed by unit name. Each snapshot replaces the whole set: units
// gone from it disappear.
interface LiveUnitsState {
  units: { [name: string]: LiveUnit };
}

const initialState: LiveUnitsState = {
  units: {},
};

const byName = (units: LiveUnit[]) =>
  units.reduce((acc: { [name: string]: LiveUnit }, unit) => {
    acc[unit.name] = unit;
    return acc;
  }, {});

export const liveUnitsSlice = createSlice({
  name: "liveUnits",
  initialState,
  reducers: {
    liveUnitsReplaced: (state, action: PayloadAction<LiveUnit[]>) => {
      state.units = byName(action.payload);
    },
  },
  extraReducers: (builder) => {
    builder.addCase(gameLoaded, (state, action) => {
      state.units = byName(action.payload.live_units ?? []);
    });
    builder.addCase(gameUnloaded, (state) => {
      state.units = {};
    });
  },
});

export const { liveUnitsReplaced } = liveUnitsSlice.actions;

export const selectLiveUnits = (state: RootState) => state.liveUnits;

export default liveUnitsSlice.reducer;
