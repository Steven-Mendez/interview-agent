# The same dashboards the local grafana/otel-lgtm container provisions from
# dashboards/, in the same folder. file(), not templatefile(): the JSON keeps
# Grafana's own ${datasource} variable for Grafana to resolve.
resource "grafana_folder" "interview_agent" {
  title = "Interview Agent"
  uid   = "interview-agent"
}

resource "grafana_dashboard" "interview_agent" {
  for_each = fileset("${path.module}/dashboards", "*.json")

  config_json = file("${path.module}/dashboards/${each.value}")
  folder      = grafana_folder.interview_agent.uid
  overwrite   = true
}
