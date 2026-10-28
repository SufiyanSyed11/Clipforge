import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#0b0d10", 900: "#12151a", 800: "#1a1e25", 700: "#262b34", 500: "#6b7280" },
        accent: { DEFAULT: "#f97316", soft: "#fdba74" },
      },
    },
  },
  plugins: [],
};
export default config;
