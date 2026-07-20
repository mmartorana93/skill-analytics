// PM2 config for the Skill Analytics dashboard.
// Start:   pm2 start ecosystem.config.js
// Reload:  pm2 restart skill-analytics
// Logs:    pm2 logs skill-analytics
//
// Paths are resolved relative to this file (__dirname) so the config is
// portable — no hardcoded home directory.
const path = require("path");

module.exports = {
  apps: [
    {
      name: "skill-analytics",
      script: "server.py",
      interpreter: "python3",
      args: "8787",
      cwd: __dirname,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      out_file: path.join(__dirname, "pm2-out.log"),
      error_file: path.join(__dirname, "pm2-error.log"),
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
