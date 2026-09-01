import tailwindcss from '@tailwindcss/postcss';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  return {
    root: 'web',
    base: env.VITE_BASE_PATH || './',
    publicDir: '../public',
    plugins: [react()],
    css: { postcss: { plugins: [tailwindcss()] } },
    build: { outDir: 'dist', emptyOutDir: true },
  };
});
