/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}'],
  theme: {
    extend: {
      colors: {
        paper: '#FBF8F2',
        ink: '#1C2B30',
        blue: {
          DEFAULT: '#2A6F97',
          deep: '#1D4E6E',
          soft: '#D9E9F2',
        },
        green: {
          DEFAULT: '#3F8362',
          deep: '#2C5F47',
          soft: '#DCEBE1',
        },
        ochre: {
          DEFAULT: '#E2954A',
          deep: '#B96F2C',
          soft: '#FBE9D6',
        },
        gold: '#D4A72C',
      },
      fontFamily: {
        display: ['Fraunces', 'ui-serif', 'Georgia', 'serif'],
        body: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      letterSpacing: {
        widest2: '.28em',
      },
      boxShadow: {
        tile: '0 12px 30px -12px rgba(28, 43, 48, 0.35)',
      },
      backgroundImage: {
        'grain': "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E\")",
      },
    },
  },
  plugins: [],
};
