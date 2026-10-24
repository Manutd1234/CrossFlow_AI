/** Shared browser tile layer. Override URL and attribution together for another provider. */
export const MAP_TILE_URL = import.meta.env.VITE_MAP_TILE_URL?.trim()
  || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';

export const MAP_TILE_ATTRIBUTION = import.meta.env.VITE_MAP_TILE_ATTRIBUTION?.trim()
  || '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>';
