import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import {defineConfig} from 'vite';

// 控制台后端（FastAPI）监听地址：与 backend/src/web/server.py 的默认端口保持一致，
// 可用 W2V_HOST / W2V_PORT 覆盖（后端读取同一组环境变量）。
const backendHost = process.env.W2V_HOST || '127.0.0.1';
const backendPort = process.env.W2V_PORT || '8123';
const backendTarget = `http://${backendHost}:${backendPort}`;

export default defineConfig(() => {
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      hmr: process.env.DISABLE_HMR !== 'true',
      watch: process.env.DISABLE_HMR === 'true' ? null : {},
      // 控制台后端（FastAPI）：/api 走 REST/SSE，/workspace 直接取工作区静态资源。
      proxy: {
        '/api': {
          target: backendTarget,
          changeOrigin: true,
        },
        '/workspace': {
          target: backendTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
