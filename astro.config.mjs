import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

export default defineConfig({
  site: 'https://culturaviva.example',
  integrations: [tailwind({ applyBaseStyles: false })],
});
