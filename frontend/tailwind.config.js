/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: "#101214",
        panel: "#181b1e",
        edge: "#2a2e33",
        ink: { DEFAULT: "#f2f2ef", dim: "#a5a49b", mute: "#6d6c66" },
        accent: "#3987e5",
        gain: "#3987e5",
        loss: "#e66767",
        warn: "#c98500",
        ok: "#199e70",
      },
    },
  },
  plugins: [],
};
