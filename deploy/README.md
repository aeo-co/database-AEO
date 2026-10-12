# Deploy services
- aeo-dashboard.service: systemd unit for the dashboard on :8000. Install:
  cp aeo-dashboard.service /etc/systemd/system/ && systemctl daemon-reload &&
  systemctl enable --now aeo-dashboard
- The MCP server (:8765) runs via aeo-mcp.service (already on the droplet).
