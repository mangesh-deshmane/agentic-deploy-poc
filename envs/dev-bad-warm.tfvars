# ADVERSARIAL TEST FILE - this must never deploy.
#
# Asks for one warm instance, which is the single most plausible way someone
# well-meaning turns a free environment into a billed one: "the demo
# cold-starts, let's keep one instance warm."
#
# Expected: Terraform's own validation rejects it before any plan is produced.
# If someone removes that validation block, the policy gate catches it at the
# plan stage instead. Two independent layers, which is the point.

project_id = "agentic-poc-5b8b"
region     = "us-central1"

app      = "app1"
customer = "demo"
env_name = "dev"

image = "ghcr.io/mangesh-deshmane/app1:sha-2a07e91"

min_instance_count = 1      # <-- the violation
max_instance_count = 2
memory             = "512Mi"
cpu                = "1"

ttl_hours = 24
protected = false
