import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

export default defineConfig({
  site: 'https://culturaviva.tech',
  integrations: [tailwind({ applyBaseStyles: false })],
});
