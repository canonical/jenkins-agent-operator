# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

output "application" {
  description = "The deployed application object."
  value       = juju_application.jenkins_agent
}

output "provides" {
  description = "Map of the provided integration endpoints."
  value = {
    agent = {
      kind     = "endpoint"
      name     = juju_application.jenkins_agent.name
      endpoint = "agent"
    }
  }
}

output "requires" {
  description = "Map of the required integration endpoints."
  value       = {}
}
