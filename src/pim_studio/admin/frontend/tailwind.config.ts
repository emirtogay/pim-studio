import type { Config } from 'tailwindcss'

// Tokens lifted verbatim from the Style Guide handoff. Don't change these
// without updating design/Style Guide.html in the same commit.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
        serif: ['"IBM Plex Serif"', 'Georgia', 'serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
      colors: {
        ink: {
          900: '#141613',
          700: '#2e3230',
          500: '#585d59',
          400: '#7c827e',
          300: '#a8aea9',
          200: '#d3d6d2',
          100: '#e7e9e5',
          50: '#f3f4f0',
        },
        paper: {
          DEFAULT: '#f7f7f2',
          card: '#ffffff',
          sunk: '#eeeee8',
        },
        brand: {
          900: '#08281f',
          800: '#0f3a2d',
          700: '#16513e',
          600: '#1e6a52',
          500: '#357a5b',
          50: '#eef3f0',
        },
        // Semantic (state) palette, kept separate from the brand accent so
        // status colour never competes with brand green.
        ok: { 600: '#2a8566', 100: '#d9e7e1' },
        warn: { 600: '#9a6a14', 100: '#f5ead0' },
        danger: { 600: '#a3362d', 100: '#f1d9d5' },
        info: { 600: '#335d82', 100: '#dde7f1' },
        hairline: '#e0e2dd',
      },
      borderRadius: {
        '2xs': '3px',
        xs: '5px',
      },
      boxShadow: {
        card: '0 1px 2px rgba(20,22,19,.04), 0 1px 1px rgba(20,22,19,.03)',
        pop: '0 8px 24px -8px rgba(20,22,19,.16), 0 2px 6px rgba(20,22,19,.06)',
      },
      maxWidth: {
        content: '1280px',
        table: '1440px',
        prose: '640px',
      },
    },
  },
  plugins: [],
} satisfies Config
