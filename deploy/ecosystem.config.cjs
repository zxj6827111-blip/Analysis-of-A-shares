/**
 * 模板：服务器上请优先运行 install_astock.sh 生成带绝对路径的版本。
 * 手工用法示例（改路径后）:
 *   pm2 start deploy/ecosystem.config.cjs
 *
 * 关键: interpreter: "none" —— script 已是 python 可执行文件
 * 重要: instances 保持 1（单进程 uvicorn + 本地任务状态）
 */
module.exports = {
  apps: [
    {
      name: "astock-serve",
      cwd: process.env.ASTOCK_APP_ROOT || __dirname + "/..",
      script: (process.env.ASTOCK_APP_ROOT || __dirname + "/..") + "/.venv/bin/python",
      args:
        "-m wtpy.apps.astock serve --host " +
        (process.env.ASTOCK_HOST || "127.0.0.1") +
        " --port " +
        (process.env.ASTOCK_PORT || "8765") +
        " --storage " +
        (process.env.ASTOCK_STORAGE ||
          (process.env.ASTOCK_APP_ROOT || __dirname + "/..") + "/storage/astock"),
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      max_restarts: 30,
      min_uptime: "5s",
      max_memory_restart: "3500M",
      env: {
        PYTHONPATH: process.env.ASTOCK_APP_ROOT || __dirname + "/..",
        PYTHONUNBUFFERED: "1",
        TZ: "Asia/Shanghai",
        TUSHARE_TOKEN: process.env.TUSHARE_TOKEN || "",
      },
      watch: false,
      time: true,
    },
  ],
};
