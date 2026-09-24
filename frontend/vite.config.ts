import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },

  // antd v5 全样式约 1MB gzip。拆 vendor 让 index 包只承载业务代码，
  // antd / echarts 的更新频率远低于业务代码，可以独立缓存。
  build: {
    chunkSizeWarningLimit: 750,
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          // dayjs 必须跟 antd 同包：antd DatePicker 等组件内部 import dayjs，
          // 若把 dayjs 拆进 vendor-state，会形成 antd → state → antd 的循环 chunk（构建告警）。
          "vendor-antd": ["antd", "@ant-design/icons", "@ant-design/cssinjs", "dayjs"],
          "vendor-charts": ["echarts", "echarts-for-react"],
          "vendor-state": ["zustand", "@tanstack/react-query", "axios"],
        },
      },
    },
  },
});
