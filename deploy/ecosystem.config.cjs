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
        // overlay_v1 增量存储 + 周五调度 + 本地治理（与 astock.env 一致）
        // 未迁移前保持 ASTOCK_MARKET_STORAGE_MODE 为空，避免未迁移就切换模式
        MARKET_DATA_ROOT: process.env.MARKET_DATA_ROOT || "",
        ASTOCK_MARKET_STORAGE_MODE: process.env.ASTOCK_MARKET_STORAGE_MODE || "",
        ASTOCK_EOD_SYNC_ENABLED: process.env.ASTOCK_EOD_SYNC_ENABLED || "",
        ASTOCK_EOD_SYNC_STARTUP: process.env.ASTOCK_EOD_SYNC_STARTUP || "",
        ASTOCK_EOD_SYNC_TIME: process.env.ASTOCK_EOD_SYNC_TIME || "",
        ASTOCK_EOD_SYNC_WEEKDAY: process.env.ASTOCK_EOD_SYNC_WEEKDAY || "",
        ASTOCK_EOD_SYNC_INDEX_ETF: process.env.ASTOCK_EOD_SYNC_INDEX_ETF || "",
        ASTOCK_MARKET_GOVERNANCE_ENABLED: process.env.ASTOCK_MARKET_GOVERNANCE_ENABLED || "",
        ASTOCK_RETENTION_GENERATIONS: process.env.ASTOCK_RETENTION_GENERATIONS || "",
        ASTOCK_RETENTION_GRACE_DAYS: process.env.ASTOCK_RETENTION_GRACE_DAYS || "",
        ASTOCK_LEGACY_RETENTION_ENABLED: process.env.ASTOCK_LEGACY_RETENTION_ENABLED || "",
        ASTOCK_LEGACY_KEEP_PER_FAMILY: process.env.ASTOCK_LEGACY_KEEP_PER_FAMILY || "",
        ASTOCK_LEGACY_MIGRATION_GRACE_DAYS: process.env.ASTOCK_LEGACY_MIGRATION_GRACE_DAYS || "",
        ASTOCK_LEGACY_MANIFEST_MIN_AGE_DAYS: process.env.ASTOCK_LEGACY_MANIFEST_MIN_AGE_DAYS || "",
        ASTOCK_CONSOLIDATE_TRADING_DAYS: process.env.ASTOCK_CONSOLIDATE_TRADING_DAYS || "",
        ASTOCK_CONSOLIDATE_DELTA_BYTES: process.env.ASTOCK_CONSOLIDATE_DELTA_BYTES || "",
        ASTOCK_CONSOLIDATE_MIN_FREE_GB: process.env.ASTOCK_CONSOLIDATE_MIN_FREE_GB || "",
        ASTOCK_CONSOLIDATE_MAX_DISK_USAGE_PCT: process.env.ASTOCK_CONSOLIDATE_MAX_DISK_USAGE_PCT || "",
        ASTOCK_CA_SYNC_WEEKDAY: process.env.ASTOCK_CA_SYNC_WEEKDAY || "",
        ASTOCK_CA_SYNC_TIME: process.env.ASTOCK_CA_SYNC_TIME || "",
      },
      watch: false,
      time: true,
    },
  ],
};
