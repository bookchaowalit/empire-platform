import { defineRailway, project, service } from "railway/iac";

// empire-platform owns only its own services in the shared project.
// Variables stay unmanaged: secrets live in Railway, never in this file.
// Every service builds from the repository Dockerfile and picks its process
// from EMPIRE_SERVICE (api, publisher, or mcp); every service answers /health.
export const partial = "empire-platform";

export default defineRailway(() => {
  const tasksApi = service("tasks-api", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
  });
  const tasksPublisher = service("tasks-publisher", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
  });

  // Remote MCP (Streamable HTTP at /mcp) behind Clerk OAuth; see docs/runbooks/remote-mcp.md.
  const empireMcp = service("empire-mcp", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
  });

  return project("empire-platform", {
    variables: { managed: false },
    resources: [tasksApi, tasksPublisher, empireMcp],
  });
});
