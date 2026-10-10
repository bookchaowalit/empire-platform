import { defineRailway, project, service } from "railway/iac";

// empire-platform owns only its own services in the shared project.
// Variables stay unmanaged: secrets live in Railway, never in this file.
// Both services build from the repository Dockerfile and pick their process
// from EMPIRE_SERVICE (api or publisher); both answer /health.
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

  return project("empire-platform", {
    variables: { managed: false },
    resources: [tasksApi, tasksPublisher],
  });
});
