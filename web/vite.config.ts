import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'
export default defineConfig({ plugins: [react(), tailwindcss()], resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } }, server: { proxy: { '^/(scopes|agent|knowledge-bases|identity|capabilities)(/|$)': { target: process.env.KB_API_URL || 'http://127.0.0.1:58080' } } } })
