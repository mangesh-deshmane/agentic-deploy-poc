# Phase 1a: the public hello image, so Terraform is proven before any app of
# yours is in the picture. Phase 1b swaps `image` for your own build.
#
# From Phase 3 onward the agent writes files exactly like this one - it never
# writes Terraform, only values that the module's own validation rules accept.

project_id = "agentic-poc-5b8b"
region     = "us-central1"

app      = "app1"
customer = "demo"
env_name = "dev"

image = "gcr.io/cloudrun/hello"

min_instance_count = 0
max_instance_count = 2
memory             = "512Mi"
cpu                = "1"

ttl_hours = 24
protected = false
