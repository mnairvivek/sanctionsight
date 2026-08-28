/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        primary: '#0A2540',
        accent: '#635BFF',
        surface: '#FFFFFF',
        bg: '#F6F9FC',
        danger: '#FF5630',
        warning: '#FFB800',
        success: '#00D924',
        muted: '#697386',
        border: '#E6E6E6',
      },
      fontFamily: {
        display: ['"DM Sans"', 'system-ui', 'sans-serif'],
        body: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.12)',
        lifted: '0 8px 24px rgba(0,0,0,.1)',
        glow: '0 0 20px rgba(99,91,255,.15)',
      },
    },
  },
  plugins: [],
};
