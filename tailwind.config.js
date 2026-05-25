/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/templates/**/*.html",
    "./app/static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        // Paleta institucional AquaG20 — tons de água
        aqua: {
          50:  "#f0f7fb",
          100: "#dceaf3",
          200: "#bcd6e7",
          300: "#8db8d4",
          400: "#5894bb",
          500: "#3a78a3",
          600: "#2f6188",
          700: "#284e6e",
          800: "#23425b",
          900: "#1f384d",
        },
      },
      fontFamily: {
        sans: ['"Inter"', "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
    },
  },
  plugins: [],
};
