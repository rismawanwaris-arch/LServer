/** @type {import('tailwindcss').Config} */
// Dibangun dengan Tailwind CLI standalone (binary) — tanpa Node, tanpa plugin npm.
module.exports = {
  content: ["./apps/**/templates/**/*.html"],
  darkMode: "media",
  theme: {
    extend: {
      fontFamily: {
        sans: ["IBM Plex Sans", "system-ui", "sans-serif"],
        mono: ["IBM Plex Mono", "ui-monospace", "monospace"],
        serif: ["IBM Plex Serif", "Georgia", "serif"],
      },
    },
  },
  plugins: [],
};
