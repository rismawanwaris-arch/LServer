/** @type {import('tailwindcss').Config} */
// Dibangun dengan Tailwind CLI standalone (binary) — tanpa Node, tanpa plugin npm.
module.exports = {
  content: ["./apps/**/templates/**/*.html"],
  // Kelas yang dirakit dinamis di template (mis. "nav-count-{{ tone }}") tidak terbaca
  // pemindai -- daftarkan di sini supaya tidak terbuang saat build.
  safelist: [
    "nav-count-mute", "nav-count-warn", "nav-count-crit", "nav-count-info",
    // Audit Data: badge-{{ tone }} dirakit dari audit.STATUS_TONES
    "badge-good", "badge-warn", "badge-crit", "badge-mute", "badge-info",
  ],
  // Tema dipilih lewat kelas .dark di <html> (tombol tema di bilah atas).
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
        serif: ['"IBM Plex Serif"', "Georgia", "serif"],
      },
      colors: {
        // Token warna (skill ui-ux-pro-max: "Data-Dense Dashboard" + palet Financial
        // Dashboard). Di-override lewat nama skala yang sudah tersebar di semua
        // template, jadi seluruh halaman ikut berganti tanpa menyentuh markup/logic:
        //  - slate  -> abu-abu netral (tidak kebiruan), permukaan & teks
        //  - indigo -> biru aksen (link, fokus, item aktif)
        slate: {
          50: '#f6f7f9',
          100: '#eef0f3',
          200: '#e4e7ec',
          300: '#d0d5dd',
          400: '#98a2b3',
          500: '#667085',
          600: '#475467',
          700: '#344054',
          800: '#1d2433',
          900: '#131926',
          950: '#0b0f18',
        },
        indigo: {
          50: '#eff4ff',
          100: '#dbe6fe',
          200: '#bfd3fe',
          300: '#93b4fd',
          400: '#6090fa',
          500: '#3b6ef6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
          950: '#172554',
        },
      },
    },
  },
  plugins: [],
};
