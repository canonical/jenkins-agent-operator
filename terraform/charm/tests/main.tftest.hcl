# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

run "setup_tests" {
  module {
    source = "./tests/setup"
  }
}

run "basic_deploy" {
  variables {
    model_uuid = run.setup_tests.model_uuid
    base       = "ubuntu@24.04"
    channel    = "latest/edge"
    # renovate: depName="jenkins-agent"
    revision = 392
  }

  assert {
    condition     = output.application.name == "jenkins-agent"
    error_message = "jenkins-agent application name did not match expected"
  }

  assert {
    condition     = output.provides.value.endpoint == "agent"
    error_message = "jenkins-agent module should provide 'provides.value' output"
  }
}
